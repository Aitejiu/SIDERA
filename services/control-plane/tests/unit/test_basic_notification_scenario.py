from __future__ import annotations

from uuid import uuid4

from tests.e2e.basic_acceptance.contracts import NotificationSnapshot
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.notification import (
    build_notification_evidence,
    run_notification,
)


def _manifest() -> dict[str, object]:
    return {
        "run_id": str(uuid4()),
        "code_commit": "a" * 40,
        "runtime_fingerprint": "b" * 64,
        "notification_route_ref": "env:SIDERA_BASIC_NOTIFICATION_ROUTE",
    }


def _snapshot(manifest: dict[str, object], **overrides: object) -> NotificationSnapshot:
    root_task_id = uuid4()
    values: dict[str, object] = {
        "run_id": manifest["run_id"],
        "root_task_id": root_task_id,
        "fact_id": uuid4(),
        "thread_id": uuid4(),
        "intent_id": uuid4(),
        "delivery_id": uuid4(),
        "attempt_id": uuid4(),
        "provider_receipt_ref": "opaque:provider-receipt-1",
        "source_ref_digest": "c" * 64,
        "private_source_ref_digest": "c" * 64,
        "web_root_task_id": root_task_id,
        "private_root_task_id": root_task_id,
        "web_subtask_id": None,
        "private_subtask_id": None,
        "web_worker_item_id": None,
        "private_worker_item_id": None,
        "route_status": "authorized",
        "route_scope_valid": True,
        "normal_business_path_observed": True,
        "gateway_claim_observed": True,
        "gateway_settle_observed": True,
        "message_has_task_context": True,
        "deep_link_targets_exact_resource": True,
        "web_mail_matches_private": True,
        "provider_status": "accepted",
        "user_acknowledged": True,
        "main_agent_re_read_latest_fact": True,
        "ack_loss_recovered": True,
        "business_state_preserved_after_recovery": True,
        "only_same_source_route_delivered": True,
    }
    values.update(overrides)
    return NotificationSnapshot.model_validate(values)


def _builder(manifest: dict[str, object]) -> EvidenceBuilder:
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario="notification",
        evidence_kind="contract",
        code_commit=str(manifest["code_commit"]),
        runtime_fingerprint=str(manifest["runtime_fingerprint"]),
    )


def test_notification_requires_all_fact_thread_and_receipt_boundaries() -> None:
    manifest = _manifest()
    evidence = build_notification_evidence(_builder(manifest), _snapshot(manifest))

    assert evidence.status == "PASS"
    assert len(evidence.notification_fact_ids) == 1
    assert len(evidence.notification_thread_ids) == 1
    assert len(evidence.notification_intent_ids) == 1
    assert len(evidence.notification_attempt_ids) == 1
    assert evidence.provider_receipt_refs == ("opaque:provider-receipt-1",)


def test_notification_unknown_route_is_incomplete_without_provider_pass() -> None:
    manifest = _manifest()
    evidence = build_notification_evidence(
        _builder(manifest),
        _snapshot(manifest, route_status="unknown", provider_status="unknown"),
    )

    assert evidence.status == "INCOMPLETE"
    assert evidence.failure_code == "notification_route_unknown"
    assert "provider_receipt_unknown" in evidence.check_codes


def test_notification_cross_task_link_is_a_failure() -> None:
    manifest = _manifest()
    evidence = build_notification_evidence(
        _builder(manifest),
        _snapshot(manifest, private_root_task_id=uuid4()),
    )

    assert evidence.status == "FAIL"
    assert evidence.failure_code == "notification_root_task_mismatch"


def test_notification_requires_a_snapshot_before_real_mode_can_pass() -> None:
    evidence = run_notification(_manifest())

    assert evidence.status == "INCOMPLETE"
    assert evidence.failure_code == "notification_snapshot_not_supplied"
