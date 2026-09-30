"""Evidence classifier for one bounded private-channel notification run."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from .contracts import NotificationSnapshot, ScenarioEvidence, WebNotificationSnapshot
from .evidence import EvidenceBuilder


def build_notification_evidence(
    builder: EvidenceBuilder,
    snapshot: NotificationSnapshot,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []

    if snapshot.source_ref_digest != snapshot.private_source_ref_digest:
        failures.append("notification_source_binding_mismatch")
    else:
        checks.append("notification_source_binding_observed")
    if snapshot.web_root_task_id != snapshot.private_root_task_id:
        failures.append("notification_root_task_mismatch")
    else:
        checks.append("notification_root_task_observed")
    if snapshot.web_subtask_id != snapshot.private_subtask_id:
        failures.append("notification_subtask_mismatch")
    if snapshot.web_worker_item_id != snapshot.private_worker_item_id:
        failures.append("notification_worker_item_mismatch")
    if snapshot.web_subtask_id == snapshot.private_subtask_id and (
        snapshot.web_subtask_id is not None
    ):
        checks.append("notification_subtask_observed")
    if snapshot.web_worker_item_id == snapshot.private_worker_item_id and (
        snapshot.web_worker_item_id is not None
    ):
        checks.append("notification_worker_item_observed")

    _require_fact(snapshot.normal_business_path_observed, "business_path", incomplete, checks)
    _require_fact(snapshot.gateway_claim_observed, "gateway_claim", incomplete, checks)
    _require_fact(snapshot.gateway_settle_observed, "gateway_settle", incomplete, checks)
    _require_fact(
        snapshot.message_has_task_context,
        "task_context_message",
        incomplete,
        checks,
    )
    _require_fact(
        snapshot.deep_link_targets_exact_resource,
        "exact_resource_deep_link",
        incomplete,
        checks,
    )
    _require_fact(
        snapshot.web_mail_matches_private,
        "web_private_thread_match",
        incomplete,
        checks,
    )
    _require_fact(
        snapshot.main_agent_re_read_latest_fact,
        "main_agent_latest_fact_read",
        incomplete,
        checks,
    )
    _require_fact(
        snapshot.only_same_source_route_delivered,
        "same_source_route_only",
        incomplete,
        checks,
    )

    if snapshot.route_status == "authorized":
        checks.append("authorized_personal_route")
    else:
        incomplete.append(f"notification_route_{snapshot.route_status}")
    if not snapshot.route_scope_valid:
        failures.append("notification_route_scope_invalid")
    else:
        checks.append("notification_route_scope_valid")

    if snapshot.provider_status == "accepted":
        checks.append("provider_receipt_accepted")
    elif snapshot.provider_status == "rejected":
        failures.append("provider_receipt_rejected")
    else:
        incomplete.append("provider_receipt_unknown")

    if snapshot.user_acknowledged:
        checks.append("user_receipt_observed")
    else:
        incomplete.append("user_receipt_not_observed")
    if snapshot.ack_loss_recovered:
        checks.append("ack_loss_recovered")
    else:
        incomplete.append("ack_loss_recovery_not_observed")
    if snapshot.business_state_preserved_after_recovery:
        checks.append("business_state_preserved_after_recovery")
    else:
        failures.append("business_state_changed_during_notification_recovery")

    failure_code = (failures or incomplete or [None])[0]
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        notification_fact_ids=(snapshot.fact_id,),
        notification_thread_ids=(snapshot.thread_id,),
        notification_intent_ids=(snapshot.intent_id,),
        notification_delivery_ids=(snapshot.delivery_id,),
        notification_attempt_ids=(snapshot.attempt_id,),
        provider_receipt_refs=(snapshot.provider_receipt_ref,),
        root_task_id=snapshot.root_task_id,
        failure_code_override=failure_code,
    )


def run_notification(
    manifest: Mapping[str, object],
    *,
    snapshot: NotificationSnapshot | None = None,
    evidence_kind: Literal["contract", "real_user_flow"] = "contract",
) -> ScenarioEvidence:
    builder = EvidenceBuilder(
        run_id=_uuid(manifest.get("run_id")),
        scenario="notification",
        evidence_kind=evidence_kind,
        code_commit=_text(manifest.get("code_commit"), "0" * 40),
        runtime_fingerprint=_text(manifest.get("runtime_fingerprint"), "0" * 64),
    )
    if snapshot is None:
        return builder.finish(
            real_tests=0,
            incomplete=True,
            check_codes=("notification_snapshot_not_supplied",),
            failure_code_override="notification_snapshot_not_supplied",
        )
    if snapshot.run_id != _uuid(manifest.get("run_id")):
        return builder.finish(
            real_tests=0,
            failed=1,
            check_codes=("notification_snapshot_run_id_mismatch",),
            failure_code_override="notification_snapshot_run_id_mismatch",
        )
    route_ref = manifest.get("notification_route_ref")
    if not isinstance(route_ref, str) or not route_ref:
        return builder.finish(
            real_tests=0,
            incomplete=True,
            check_codes=("notification_route_reference_missing",),
            failure_code_override="notification_route_reference_missing",
        )
    return build_notification_evidence(builder, snapshot)


def build_web_notification_evidence(
    builder: EvidenceBuilder,
    snapshot: WebNotificationSnapshot,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []
    if snapshot.channel != "web_inbox":
        failures.append("web_notification_channel_mismatch")
    if snapshot.deep_link_resource_id != snapshot.worker_item_id and (
        snapshot.worker_item_id is not None
    ):
        failures.append("web_notification_deep_link_mismatch")
    elif snapshot.deep_link_resource_id not in {
        snapshot.root_task_id,
        snapshot.subtask_id,
        snapshot.worker_item_id,
    }:
        failures.append("web_notification_deep_link_unbound")
    else:
        checks.append("web_notification_deep_link_observed")
    _require_fact(snapshot.event_observed, "web_event", incomplete, checks)
    _require_fact(snapshot.inbox_created, "web_inbox_item", incomplete, checks)
    _require_fact(snapshot.exact_mapping, "web_exact_mapping", incomplete, checks)
    _require_fact(snapshot.user_read, "web_read", incomplete, checks)
    _require_fact(snapshot.user_acknowledged, "web_ack", incomplete, checks)
    _require_fact(
        snapshot.task_state_preserved,
        "web_task_state_preserved",
        incomplete,
        checks,
    )
    if snapshot.duplicate_count != 0:
        failures.append("web_notification_duplicate")
    else:
        checks.append("web_notification_deduplicated")
    failure_code = (failures or incomplete or [None])[0]
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        notification_fact_ids=(snapshot.event_id,),
        notification_thread_ids=(snapshot.inbox_item_id,),
        root_task_id=snapshot.root_task_id,
        failure_code_override=failure_code,
    )


def run_web_notification(
    manifest: Mapping[str, object],
    *,
    snapshot: WebNotificationSnapshot | None = None,
    evidence_kind: Literal["contract", "real_user_flow"] = "contract",
) -> ScenarioEvidence:
    builder = EvidenceBuilder(
        run_id=_uuid(manifest.get("run_id")),
        scenario="web-notification",
        evidence_kind=evidence_kind,
        code_commit=_text(manifest.get("code_commit"), "0" * 40),
        runtime_fingerprint=_text(manifest.get("runtime_fingerprint"), "0" * 64),
    )
    if snapshot is None:
        return builder.finish(
            real_tests=0,
            incomplete=True,
            check_codes=("web_notification_snapshot_not_supplied",),
            failure_code_override="web_notification_snapshot_not_supplied",
        )
    if snapshot.run_id != _uuid(manifest.get("run_id")):
        return builder.finish(
            real_tests=0,
            failed=1,
            check_codes=("web_notification_snapshot_run_id_mismatch",),
            failure_code_override="web_notification_snapshot_run_id_mismatch",
        )
    return build_web_notification_evidence(builder, snapshot)


def _require_fact(
    value: bool,
    name: str,
    incomplete: list[str],
    checks: list[str],
) -> None:
    if value:
        checks.append(f"{name}_observed")
    else:
        incomplete.append(f"{name}_not_observed")


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest run_id is invalid") from exc


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
