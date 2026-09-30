from __future__ import annotations

from uuid import UUID

import pytest

from sidera_control_plane.demo import heartbeat
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.http_client import DemoClientNotFound
from sidera_control_plane.demo.state import DemoStateStore, StateCorrupt
from sidera_control_plane.demo.supervisor import ProcessIdentityError, write_run_phase
from tests.unit.interview_demo_support import manifest_for


class HeartbeatClient:
    def __init__(self, manifest: DemoManifest, registered: set[UUID]) -> None:
        self.manifest = manifest
        self.registered = registered
        self.workers = {
            worker_id: {"id": str(worker_id), "workspace_id": str(manifest.workspace_id), "version": 1}
            for worker_id in registered
        }
        self.results: dict[str, dict[str, object]] = {}
        self.commands: list[dict[str, object]] = []

    def get(self, path: str) -> object:
        parts = path.split("?")[0].split("/")
        if "/commands/" in path:
            command_id = parts[-1]
            result = self.results.get(command_id)
            if result is None:
                raise DemoClientNotFound("not found")
            return {"command_id": command_id, "status": "applied", "result": result}
        worker_id = UUID(parts[-1])
        if worker_id not in self.workers:
            raise DemoClientNotFound("not found")
        return dict(self.workers[worker_id])

    def command(self, envelope: dict[str, object]) -> dict[str, object]:
        self.commands.append(dict(envelope))
        command_id = str(envelope["command_id"])
        target_id = str(envelope["target_id"])
        self.workers[UUID(target_id)]["version"] += 1
        result = {
            "command_id": command_id,
            "aggregate_id": target_id,
            "aggregate_version": self.workers[UUID(target_id)]["version"],
            "event_ids": [],
            "data": {},
        }
        self.results[command_id] = result
        return result


def test_heartbeat_only_targets_manifest_workers_that_are_registered(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    expected_worker = manifest.worker_ids["validation"]
    client = HeartbeatClient(manifest, {expected_worker})
    monkeypatch.setattr(heartbeat, "_heartbeat_cycle", lambda: 42)

    reported = heartbeat.heartbeat_registered_workers(manifest, client)

    assert reported == (expected_worker,)
    assert len(client.commands) == 1
    assert client.commands[0]["command_type"] == "HeartbeatWorker"
    assert client.commands[0]["payload"] == {"healthy": True}
    assert all(command["target_id"] in {str(value) for value in manifest.worker_ids.values()} for command in client.commands)


def test_duplicate_heartbeat_cycle_does_not_issue_a_second_command(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    worker_id = manifest.worker_ids["validation"]
    client = HeartbeatClient(manifest, {worker_id})
    monkeypatch.setattr(heartbeat, "_heartbeat_cycle", lambda: 42)

    first = heartbeat.heartbeat_registered_workers(manifest, client)
    second = heartbeat.heartbeat_registered_workers(manifest, client)

    assert first == second == (worker_id,)
    assert len(client.commands) == 1
    assert len(client.workers) == 1


def test_paused_output_gate_does_not_suspend_worker_heartbeats(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    worker_id = manifest.worker_ids["validation"]
    client = HeartbeatClient(manifest, {worker_id})
    store = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    write_run_phase(manifest, "running")
    for gate in ("parallel", "before-artifact", "handoff"):
        store.set_gate(gate, False)
    monkeypatch.setattr(heartbeat, "build_demo_process_specs", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        heartbeat.DemoSupervisor,
        "check",
        lambda _self: {"control-plane": True},
    )
    monkeypatch.setattr(heartbeat, "_heartbeat_cycle", lambda: 43)

    heartbeat._validate_heartbeat_runtime(manifest, store)
    reported = heartbeat.heartbeat_registered_workers(manifest, client)

    assert reported == (worker_id,)
    assert store.read_gate("parallel") is False


def test_corrupt_demo_state_stops_heartbeat_runtime(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    store = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    write_run_phase(manifest, "running")
    store.gates_path.write_text("{", encoding="utf-8")

    with pytest.raises(StateCorrupt):
        heartbeat._validate_heartbeat_runtime(manifest, store)


def test_missing_process_identity_stops_heartbeat_runtime(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    store = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    write_run_phase(manifest, "running")
    monkeypatch.setattr(heartbeat, "build_demo_process_specs", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        heartbeat.DemoSupervisor,
        "check",
        lambda _self: {"control-plane": False},
    )

    with pytest.raises(ProcessIdentityError):
        heartbeat._validate_heartbeat_runtime(manifest, store)
