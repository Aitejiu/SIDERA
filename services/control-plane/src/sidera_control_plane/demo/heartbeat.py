from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID, uuid5

from .contracts import DemoManifest
from .environment import runtime_root_for
from .http_client import (
    DemoClient,
    DemoClientError,
    DemoClientNotFound,
    DemoCommandRejected,
)
from .manifest import load_manifest, validate_manifest
from .state import DemoStateStore, OperationRecord, StateCorrupt, StateIdentityConflict
from .supervisor import (
    DemoSupervisor,
    ProcessIdentityError,
    SupervisorError,
    build_demo_process_specs,
    read_run_phase,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_HEARTBEAT_SECONDS = 5
_GATES = ("parallel", "before-artifact", "handoff", "governance")


def heartbeat_registered_workers(
    manifest: DemoManifest, client: DemoClient
) -> tuple[UUID, ...]:
    cycle = _heartbeat_cycle()
    state = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    reported: list[UUID] = []

    for worker_id in manifest.worker_ids.values():
        command_id = uuid5(manifest.run_id, f"heartbeat:{worker_id}:{cycle}")
        key = f"heartbeat:{worker_id}:{cycle}"
        stored = state.read(key)
        if stored is not None:
            record = OperationRecord.from_dict(stored)
            if record.status == "applied":
                reported.append(worker_id)
            continue

        try:
            worker = client.get(
                f"/api/v1/workers/{worker_id}?workspace_id={manifest.workspace_id}"
            )
        except DemoClientNotFound:
            continue
        version = _registered_worker_version(worker, manifest, worker_id)
        if version is None:
            continue

        envelope: dict[str, object] = {
            "command_id": str(command_id),
            "workspace_id": str(manifest.workspace_id),
            "command_type": "HeartbeatWorker",
            "target_type": "worker",
            "actor": {"type": "local", "id": "aitejiu"},
            "target_id": str(worker_id),
            "expected_version": version,
            "idempotency_key": str(command_id),
            "correlation_id": str(command_id),
            "payload": {"healthy": True},
        }
        reservation = state.reserve(key, command_id, envelope)
        if reservation.status == "applied":
            reported.append(worker_id)
            continue
        if reservation.status != "pending":
            continue

        try:
            result = client.command(envelope)
        except DemoCommandRejected:
            state.settle(key, command_id, "rejected", {"status": "rejected"})
            continue
        if result.get("command_id") != str(command_id):
            state.settle(key, command_id, "unknown", {})
            raise DemoClientError("heartbeat_ack_identity_mismatch")
        state.settle(key, command_id, "applied", result)
        reported.append(worker_id)

    return tuple(reported)


def main() -> int:
    raw_manifest = os.environ.get("SIDERA_INTERVIEW_DEMO_MANIFEST")
    if raw_manifest is None:
        print("heartbeat_manifest_required", file=sys.stderr)
        return 2
    manifest_path = Path(raw_manifest).expanduser()
    try:
        manifest = load_manifest(manifest_path)
        validate_manifest(manifest, _PROJECT_ROOT)
        state = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    except (OSError, ValueError) as exc:
        print(f"heartbeat_configuration_invalid:{type(exc).__name__}", file=sys.stderr)
        return 2

    client = DemoClient(manifest)
    print(
        json.dumps(
            {"component": "demo-worker-heartbeat", "status": "ready"},
            separators=(",", ":"),
        ),
        flush=True,
    )
    started_at = time.monotonic()
    try:
        while True:
            time.sleep(_HEARTBEAT_SECONDS)
            phase = read_run_phase(manifest)
            if phase in {"stopping", "stopped", "failed"}:
                return 0
            if phase == "starting":
                if time.monotonic() - started_at > 60:
                    raise SupervisorError("demo_startup_phase_timeout")
                continue
            if phase != "running":
                raise ProcessIdentityError("demo_run_phase_invalid")

            _validate_heartbeat_runtime(manifest, state)
            heartbeat_registered_workers(manifest, client)
    except (
        DemoClientError,
        OSError,
        ProcessIdentityError,
        StateCorrupt,
        StateIdentityConflict,
        SupervisorError,
    ) as exc:
        print(f"heartbeat_stopped:{type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        client.close()


def _heartbeat_cycle() -> int:
    return int(time.time() // _HEARTBEAT_SECONDS)


def _validate_heartbeat_runtime(
    manifest: DemoManifest, state: DemoStateStore
) -> None:
    if read_run_phase(manifest) != "running":
        raise ProcessIdentityError("demo_run_phase_invalid")
    for gate in _GATES:
        state.read_gate(gate)
    process_states = DemoSupervisor(
        manifest,
        specs=build_demo_process_specs(manifest, include_environment=False),
    ).check()
    if not process_states or not all(process_states.values()):
        raise ProcessIdentityError("demo_process_identity_invalid")


def _registered_worker_version(
    value: object, manifest: DemoManifest, worker_id: UUID
) -> int | None:
    if not isinstance(value, dict):
        return None
    version = value.get("version")
    if (
        value.get("id") != str(worker_id)
        or value.get("workspace_id") != str(manifest.workspace_id)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        return None
    return version


if __name__ == "__main__":
    raise SystemExit(main())
