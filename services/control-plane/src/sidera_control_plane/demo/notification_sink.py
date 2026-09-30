from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryEnvelope,
)

from .contracts import DEMO_NOTIFICATION_SOURCE_REF, DemoManifest
from .environment import runtime_root_for
from .faults import consume_fault_once
from .manifest import load_manifest, validate_manifest

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_POLL_SECONDS = 2.0


def deliver_claimed_notification(
    manifest: DemoManifest, envelope: NotificationDeliveryEnvelope
) -> str:
    """Persist one claimed delivery at the demo's external notification boundary."""
    if (
        envelope.channel_type != "local"
        or envelope.source_ref != DEMO_NOTIFICATION_SOURCE_REF
    ):
        raise ValueError("demo notification route is not the local demo route")
    root = runtime_root_for(manifest) / "notifications"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    message_id = f"demo-message:{envelope.delivery_id}"
    path = root / f"message-{envelope.delivery_id}.json"
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise ValueError("demo notification message path is not a regular file")
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("message_id") != message_id:
            raise ValueError("demo notification delivery identity conflict")
        return message_id
    payload = {
        "schema_version": 1,
        "message_id": message_id,
        "delivery_id": str(envelope.delivery_id),
        "notification_id": str(envelope.notification_id),
        "thread_id": str(envelope.thread_id),
        "deep_link": envelope.deep_link,
        "envelope": envelope.to_mapping(),
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return message_id


def deliver_once(manifest: DemoManifest) -> tuple[str, ...]:
    with httpx.Client(timeout=5.0, trust_env=False, follow_redirects=False) as client:
        claim_url = (
            f"{manifest.endpoints.control_plane}/api/v1/workspaces/"
            f"{manifest.workspace_id}/notification-deliveries/claims"
        )
        response = client.post(
            claim_url,
            json={
                "module_instance_id": str(manifest.module_ids["notification"]),
                "supported_contract_versions": [1],
                "batch_size": 20,
                "lease_seconds": 5 if manifest.scenario == "controls" else 30,
            },
        )
        response.raise_for_status()
        raw = response.json()
        deliveries = raw.get("deliveries", []) if isinstance(raw, dict) else []
        message_ids: list[str] = []
        for item in deliveries:
            envelope = _envelope_from_mapping(item)
            message_id = deliver_claimed_notification(manifest, envelope)
            if manifest.scenario == "controls" and consume_fault_once(
                manifest,
                "notification-ack-lost",
                envelope.delivery_id,
            ):
                message_ids.append(message_id)
                continue
            ack_url = (
                f"{manifest.endpoints.control_plane}/api/v1/workspaces/"
                f"{manifest.workspace_id}/notification-deliveries/"
                f"{envelope.delivery_id}/ack"
            )
            ack = client.post(
                ack_url,
                json={
                    "claim_token": str(envelope.claim_token),
                    "fencing_token": envelope.fencing_token,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "provider_message_ref": message_id,
                },
            )
            ack.raise_for_status()
            message_ids.append(message_id)
        return tuple(message_ids)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local SIDERA demo notification sink")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(Path(args.manifest))
        validate_manifest(manifest, _PROJECT_ROOT)
        print(json.dumps({"component": "demo-notification-sink", "status": "ready"}), flush=True)
        if args.once:
            print(
                json.dumps(
                    {
                        "component": "demo-notification-sink",
                        "status": "ok",
                        "messages": list(deliver_once(manifest)),
                    }
                ),
                flush=True,
            )
            return 0
        while True:
            try:
                deliver_once(manifest)
            except Exception as exc:  # noqa: BLE001 - keep the sink alive while dependencies recover
                print(
                    json.dumps(
                        {
                            "component": "demo-notification-sink",
                            "status": "poll_error",
                            "error_code": type(exc).__name__,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(_POLL_SECONDS)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - stable CLI boundary
        print(
            json.dumps(
                {"component": "demo-notification-sink", "status": "error", "error_code": type(exc).__name__},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


def _envelope_from_mapping(value: Any) -> NotificationDeliveryEnvelope:
    if not isinstance(value, dict):
        raise TypeError("notification delivery envelope is invalid")
    uuid_fields = ("delivery_id", "notification_id", "thread_id", "claim_token")
    parsed: dict[str, Any] = dict(value)
    for field in uuid_fields:
        parsed[field] = UUID(str(parsed[field]))
    parsed["subtask_path"] = tuple(str(item) for item in parsed.get("subtask_path", ()))
    parsed["created_at"] = datetime.fromisoformat(str(parsed["created_at"]))
    return NotificationDeliveryEnvelope(**parsed)


if __name__ == "__main__":
    raise SystemExit(main())
