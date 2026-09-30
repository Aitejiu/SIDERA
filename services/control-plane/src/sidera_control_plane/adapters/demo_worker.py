from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
)
from sidera_control_plane.application.ports import WorkerAdapter
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.faults import consume_fault_once
from sidera_control_plane.demo.knowledge import demo_knowledge_candidate
from sidera_control_plane.demo.state import DemoStateStore, OperationRecord
from sidera_control_plane.demo.worker_handoff import (
    capture_checkpoint,
    checkpoint_for,
    context_binding_from_session,
    enable_writer,
    inspect_operation,
    is_handoff_target,
    stopped_delivery,
    writer_enabled,
)
from sidera_control_plane.demo.worker_steps import (
    advance_worker,
    artifact_candidates,
    receive_worker_input,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
    ContextMaterialization,
    ContextMaterializationV1,
    ContextMaterializationV2,
    ContextMaterializationV3,
    ContextPurpose,
)
from sidera_control_plane.domain.context_patches import (
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
)
from sidera_control_plane.domain.gateway import (
    AdapterEvent,
    AdapterSessionHandle,
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffOperationInspection,
    SessionContextBinding,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    HandoffCheckpointMode,
    IdempotencyCapability,
    ModuleInstance,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.workspace import (
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    workspace_manifest_digest,
)


class DemoWorkerAdapterError(RuntimeError):
    """Raised when a demo Worker operation cannot be safely represented."""


_GIT_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


class DemoWorkerAdapter(WorkerAdapter):
    """Durable, deterministic Worker boundary for the interview demo."""

    provider_ref = "interview-demo-worker"
    _CAPABILITIES = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
        context_package_versions=(1, 2, 3),
        turn_completion_versions=(1,),
        handoff_checkpoint_mode=HandoffCheckpointMode.CONTROL_EVENT,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
    )

    def __init__(
        self,
        manifest: DemoManifest,
        instance: ModuleInstance,
        workspace_locator: Callable[[ExecutionWorkspaceManifest], Path],
    ) -> None:
        if instance.workspace_id != manifest.workspace_id:
            raise ValueError("demo worker module identity has a different workspace")
        if instance.id not in {
            manifest.module_ids["worker_primary"],
            manifest.module_ids["worker_secondary"],
        }:
            raise ValueError("demo worker module identity is not in the manifest")
        if instance.config.get("worker.executable") != "demo":
            raise ValueError("demo worker module identity has a different executable")
        self.manifest = manifest
        self.instance = instance
        self._workspace_locator = workspace_locator
        self._state = DemoStateStore(
            manifest.run_root / "worker-state" / instance.id.hex,
            manifest.run_id,
        )

    @classmethod
    def capabilities(cls) -> WorkerAdapterCapabilityProfile:
        return cls._CAPABILITIES

    def probe(self, config: Mapping[str, object]) -> WorkerAdapterCapabilityProfile:
        configured_provider = config.get("worker.provider")
        if configured_provider is not None and configured_provider != self.provider_ref:
            raise ValueError("demo worker provider is fixed")
        return self._CAPABILITIES

    def accept_context(
        self,
        materialization: ContextMaterialization,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        _validate_context(materialization, operation_id, self._CAPABILITIES)
        metadata = materialization.metadata
        return ContextAcknowledgement(
            package_id=metadata.package_id,
            package_hash=metadata.package_hash,
            consumer_type=metadata.consumer_type,
            consumer_id=metadata.consumer_id,
            context_version=metadata.context_version,
            adapter_module_id=self.instance.id,
            adapter_config_version=self.instance.config_version,
            acknowledged_at=datetime.now(UTC),
            schema_version=materialization.schema_version,
            operation_id=operation_id,
        )

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> AdapterSessionHandle:
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        if spec.adapter_module_ref != self.instance.id:
            raise DemoWorkerAdapterError("adapter module identity mismatch")
        if spec.workspace_id != self.manifest.workspace_id:
            raise DemoWorkerAdapterError("execution workspace identity mismatch")
        if workspace.workspace_id != self.manifest.workspace_id:
            raise DemoWorkerAdapterError("workspace manifest identity mismatch")
        if workspace.execution_id != spec.execution_id:
            raise DemoWorkerAdapterError("workspace execution identity mismatch")
        if workspace.task_id != spec.task_id:
            raise DemoWorkerAdapterError("workspace task identity mismatch")
        if workspace.fencing_token != spec.fencing_token:
            raise DemoWorkerAdapterError("workspace fencing identity mismatch")
        if len(workspace.checkouts) != 1:
            raise DemoWorkerAdapterError("workspace checkout identity mismatch")
        checkout = workspace.checkouts[0]
        requested_checkout = spec.repositories[0]
        if (
            checkout.repository_ref not in _allowed_repository_refs(self.manifest)
            or requested_checkout.repository_ref
            not in _allowed_repository_refs(self.manifest)
            or checkout.base_revision != self.manifest.base_commit
            or checkout.base_revision != requested_checkout.base_revision
            or checkout.current_head != self.manifest.base_commit
        ):
            raise DemoWorkerAdapterError("workspace checkout identity mismatch")
        if spec.context_package is not None and (
            spec.context_package.metadata.consumer_id != spec.execution_id
        ):
            raise DemoWorkerAdapterError("execution context consumer identity mismatch")
        worker_item_key = _worker_item_key(
            self.manifest,
            spec.worker_id,
            goal=spec.goal,
            request_purpose=spec.request_purpose,
        )
        handoff_target = _is_handoff_target(self.manifest, spec)
        handoff_instruction = (
            _handoff_instruction(spec.context_package) if handoff_target else None
        )
        unresolved_workspace_path = self._workspace_locator(workspace).expanduser()
        if unresolved_workspace_path.is_symlink():
            raise DemoWorkerAdapterError("execution workspace cannot be a symlink")
        workspace_path = unresolved_workspace_path.resolve()
        if not workspace_path.exists() or not workspace_path.is_dir():
            raise DemoWorkerAdapterError("execution workspace is unavailable")

        runtime_head = _workspace_runtime_head(spec, workspace)
        session_id = uuid5(
            self.manifest.run_id,
            f"demo-worker-session:{self.instance.id}:{spec.execution_id}",
        )
        adapter_session_id = f"interview-demo-session-{spec.execution_id}"
        process_ref = f"interview-demo://{self.manifest.run_id}/{adapter_session_id}"
        digest = workspace_manifest_digest(workspace)
        request: dict[str, object] = {
            "execution_id": str(spec.execution_id),
            "workspace_id": str(spec.workspace_id),
            "worker_id": str(spec.worker_id),
            "adapter_module_id": str(spec.adapter_module_ref),
            "workspace_module_id": str(spec.workspace_module_ref),
            "workspace_digest": digest,
        }
        key = f"session/{spec.execution_id}"
        record = self._state.reserve(key, operation_id, request)
        if record.status == "applied":
            self._ensure_session_index(session_id, key, spec.execution_id)
            return self._handle_from_record(record)
        if record.status == "unknown":
            start_fact = next(
                (
                    event
                    for event in self._state.events_after(session_id, 0)
                    if event.operation_id == operation_id
                    and event.event_type == "session.started"
                ),
                None,
            )
            if start_fact is None:
                raise DemoWorkerAdapterError("session_start_outcome_unknown")

        if handoff_target and consume_fault_once(
            self.manifest,
            "target-start-unknown",
            operation_id,
        ):
            self._state.settle(
                key,
                operation_id,
                "unknown",
                {
                    "execution_id": str(spec.execution_id),
                    "worker_id": str(spec.worker_id),
                    "adapter_module_id": str(self.instance.id),
                    "outcome": "unknown",
                    "reason": "target_start_unknown",
                },
            )
            raise DemoWorkerAdapterError("target_start_unknown")

        context_binding = (
            _target_context_binding(
                spec,
                session_id=session_id,
                operation_id=operation_id,
            )
            if handoff_target
            else None
        )

        event = self._state.append_event(
            session_id,
            operation_id,
            "session.started",
            {
                "execution_id": str(spec.execution_id),
                "worker_id": str(spec.worker_id),
                "adapter_module_id": str(self.instance.id),
                "workspace_digest": digest,
                "simulation": True,
            },
        )
        self._state.settle(
            key,
            operation_id,
            "applied",
            {
                "execution_session_id": str(session_id),
                "adapter_session_id": adapter_session_id,
                "process_ref": process_ref,
                "started_at": event.occurred_at.isoformat(),
                "workspace_digest": digest,
                "adapter_module_id": str(self.instance.id),
                "context_identity": _context_identity(spec.context_package),
                "context_binding": (
                    context_binding.to_mapping() if context_binding is not None else None
                ),
                "result_inputs": _result_inputs(spec.context_package),
                "worker_item_key": worker_item_key,
                "worker_id": str(spec.worker_id),
                "workspace_id": str(spec.workspace_id),
                "task_id": str(spec.task_id),
                "execution_id": str(spec.execution_id),
                "fencing_token": spec.fencing_token,
                "workspace_path": str(workspace_path),
                "workspace_manifest": _workspace_manifest_record(workspace),
                "workspace_runtime_head": runtime_head,
                "start_operation_id": str(operation_id),
                "handoff_held": handoff_target,
                "handoff_instruction": handoff_instruction,
                "acceptance_criteria": list(spec.acceptance_criteria),
                "result_derivation_kind": (
                    "rework"
                    if _is_rework_context(spec.context_package)
                    else None
                ),
            },
        )
        self._ensure_session_index(session_id, key, spec.execution_id)
        return AdapterSessionHandle(
            execution_session_id=session_id,
            adapter_session_id=adapter_session_id,
            process_ref=process_ref,
            capability_snapshot=self._CAPABILITIES,
            last_accepted_sequence=0,
            started_at=event.occurred_at,
            context_binding=context_binding,
        )

    def read_events(self, handle: AdapterSessionHandle) -> Iterable[AdapterEvent]:
        session = self._require_session(handle)
        checkpointed = checkpoint_for(self._state, handle.execution_session_id) is not None
        if _has_worker_step_state(session) and not checkpointed and (
            not is_handoff_target(session)
            or writer_enabled(self._state, handle.execution_session_id)
        ):
            advance_worker(self.manifest, handle.execution_session_id)
        return tuple(
            AdapterEvent(
                event_type=event.event_type,
                sequence=event.sequence,
                payload=event.payload,
                occurred_at=event.occurred_at,
                raw_ref=(
                    f"demo-state://{handle.execution_session_id}/{event.sequence}"
                ),
            )
            for event in self._state.events_after(
                handle.execution_session_id, handle.last_accepted_sequence
            )
        )

    def stop_session(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        session = self._require_session(handle)
        handoff_result = stopped_delivery(
            self._state,
            session,
            operation_id=operation_id,
        )
        if handoff_result is not None:
            return handoff_result
        key = f"stop/{handle.execution_session_id}/{operation_id}"
        record = self._state.reserve(
            key,
            operation_id,
            {"execution_session_id": str(handle.execution_session_id)},
        )
        if record.status == "applied":
            return _delivery_from_record(
                record, operation_id, handle.execution_session_id
            )

        existing_stop = next(
            (
                event
                for event in self._state.events_after(handle.execution_session_id, 0)
                if event.event_type == "session.stopped"
            ),
            None,
        )
        event = existing_stop or self._state.append_event(
            handle.execution_session_id,
            operation_id,
            "session.stopped",
            {
                "execution_session_id": str(handle.execution_session_id),
                "simulation": True,
            },
        )
        self._state.settle(
            key,
            operation_id,
            "applied",
            {
                "execution_session_id": str(handle.execution_session_id),
                "accepted_sequence": event.sequence,
                "outcome": "delivered",
            },
        )
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.DELIVERED,
            accepted_sequence=event.sequence,
            failure_code=None,
            execution_session_id=handle.execution_session_id,
        )

    def collect_artifacts(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
    ) -> tuple[ArtifactCandidate, ...]:
        del operation_id
        session = self._require_session(handle)
        if not _has_worker_step_state(session):
            return ()
        return artifact_candidates(self.manifest, handle.execution_session_id)

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        session = self._require_session(handle)
        if checkpoint_for(self._state, handle.execution_session_id) is not None:
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.BLOCKED,
                accepted_sequence=None,
                failure_code="handoff_checkpointed",
                execution_session_id=handle.execution_session_id,
            )
        if is_handoff_target(session) and not writer_enabled(
            self._state, handle.execution_session_id
        ):
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.BLOCKED,
                accepted_sequence=None,
                failure_code="handoff_writer_disabled",
                execution_session_id=handle.execution_session_id,
            )
        if session.get("context_identity") is None:
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.BLOCKED,
                accepted_sequence=None,
                failure_code="worker_context_missing",
                execution_session_id=handle.execution_session_id,
            )
        if not _has_worker_step_state(session):
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.BLOCKED,
                accepted_sequence=None,
                failure_code="worker_session_step_state_missing",
                execution_session_id=handle.execution_session_id,
            )
        if operation_id == gateway_operation_id(
            UUID(str(session["execution_id"])),
            GatewayOperationType.SEND_INPUT,
        ):
            event = self._state.append_event(
                handle.execution_session_id,
                operation_id,
                "prompt.accepted",
                {
                    "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "simulation": True,
                },
            )
            self._state.append_event(
                handle.execution_session_id,
                uuid5(operation_id, "sidera.demo.turn-completed:v1"),
                "turn.completed",
                {
                    "schema_version": 1,
                    "operation_id": str(operation_id),
                    "stop_reason": "end_turn",
                },
            )
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=event.sequence,
                failure_code=None,
                execution_session_id=handle.execution_session_id,
            )
        return receive_worker_input(
            self.manifest,
            self._state,
            session,
            handle,
            text,
            attachment_refs,
            operation_id,
        )

    def apply_context_patch(
        self,
        handle: AdapterSessionHandle,
        materialization: ContextPatchMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextPatchDeliveryResult:
        del handle, materialization, operation_id
        raise DemoWorkerAdapterError(
            "apply_context_patch is unsupported until the demo step task"
        )

    def collect_knowledge_candidates(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
        worker_item_id: UUID | None = None,
        source_artifact_ids: tuple[UUID, ...] = (),
    ) -> tuple[KnowledgeCandidateProposalV1, ...]:
        del operation_id
        if self.manifest.scenario != "knowledge":
            raise DemoWorkerAdapterError(
                "collect_knowledge_candidates is unsupported outside the knowledge scenario"
            )
        session = self._require_session(handle)
        if session.get("worker_item_key") != "validate":
            return ()
        if worker_item_id is None or not source_artifact_ids:
            return ()
        return (demo_knowledge_candidate(worker_item_id, source_artifact_ids[0]),)

    def capture_handoff_checkpoint(
        self,
        handle: AdapterSessionHandle,
        *,
        transition_id: UUID,
        source_input_cutoff_sequence: int,
        operation_id: UUID,
    ) -> HandoffCheckpointEvidence:
        return capture_checkpoint(
            self._state,
            self._require_session(handle),
            transition_id=transition_id,
            source_input_cutoff_sequence=source_input_cutoff_sequence,
            operation_id=operation_id,
        )

    def inspect_handoff_operation(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
    ) -> HandoffOperationInspection:
        return inspect_operation(
            self._state,
            self._require_session(handle),
            operation_id=operation_id,
            context_binding=handle.context_binding,
        )

    def enable_handoff_writer(
        self,
        handle: AdapterSessionHandle,
        *,
        fencing_token: int,
        operation_id: UUID,
    ) -> DeliveryResult:
        return enable_writer(
            self._state,
            self._require_session(handle),
            fencing_token=fencing_token,
            operation_id=operation_id,
            context_binding=handle.context_binding,
        )

    def _require_session(self, handle: AdapterSessionHandle) -> dict[str, object]:
        if not isinstance(handle, AdapterSessionHandle):
            raise DemoWorkerAdapterError("session handle is invalid")
        raw_record = self._state.read(f"session/{_execution_id_from_session(handle)}")
        if raw_record is None:
            raise DemoWorkerAdapterError("session is not persisted")
        record = OperationRecord.from_dict(raw_record)
        stored_session_id = record.result.get("execution_session_id")
        if stored_session_id != str(handle.execution_session_id):
            raise DemoWorkerAdapterError("session identity mismatch")
        if (
            record.result.get("adapter_session_id") != handle.adapter_session_id
            or record.result.get("process_ref") != handle.process_ref
        ):
            raise DemoWorkerAdapterError("session handle binding mismatch")
        return record.result

    def _ensure_session_index(
        self, session_id: UUID, session_key: str, execution_id: UUID
    ) -> None:
        key = f"session-index/{session_id}"
        operation_id = uuid5(
            NAMESPACE_URL,
            f"sidera.demo.session-index:{self.manifest.run_id}:{self.instance.id}:{session_id}",
        )
        request: dict[str, object] = {
            "session_id": str(session_id),
            "session_key": session_key,
            "execution_id": str(execution_id),
            "module_id": str(self.instance.id),
        }
        record = self._state.reserve(key, operation_id, request)
        result: dict[str, object] = {
            "session_id": str(session_id),
            "session_key": session_key,
            "execution_id": str(execution_id),
        }
        if record.status == "pending":
            self._state.settle(key, operation_id, "applied", result)
        elif record.status != "applied" or record.result != result:
            raise DemoWorkerAdapterError("persisted session index conflicts")

    def _handle_from_record(self, record: OperationRecord) -> AdapterSessionHandle:
        result = record.result
        try:
            session_id = UUID(str(result["execution_session_id"]))
            adapter_session_id = str(result["adapter_session_id"])
            process_ref = str(result["process_ref"])
            started_at = datetime.fromisoformat(str(result["started_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DemoWorkerAdapterError("persisted session facts are corrupt") from exc
        return AdapterSessionHandle(
            execution_session_id=session_id,
            adapter_session_id=adapter_session_id,
            process_ref=process_ref,
            capability_snapshot=self._CAPABILITIES,
            last_accepted_sequence=0,
            started_at=started_at,
            context_binding=context_binding_from_session(result),
        )


def _validate_context(
    materialization: ContextMaterialization,
    operation_id: UUID,
    profile: WorkerAdapterCapabilityProfile,
) -> None:
    expected_type = {
        1: ContextMaterializationV1,
        2: ContextMaterializationV2,
        3: ContextMaterializationV3,
    }.get(materialization.schema_version)
    if expected_type is None or not isinstance(materialization, expected_type):
        raise TypeError("materialization schema does not match its typed payload")
    if not isinstance(operation_id, UUID):
        raise TypeError("operation_id must be a UUID")
    if not profile.supports_context_package_version(materialization.schema_version):
        raise ValueError("context package version is not declared by demo worker")
    if (
        materialization.metadata.purpose is not ContextPurpose.EXECUTION
        or materialization.metadata.consumer_type is not ContextConsumerType.WORKER
    ):
        raise ValueError("demo worker requires an execution worker context")
    if materialization.total_byte_count > 256 * 1024:
        raise ValueError("context materialization exceeds demo worker size limit")


def _context_identity(
    materialization: ContextMaterialization | None,
) -> dict[str, object] | None:
    if materialization is None:
        return None
    metadata = materialization.metadata
    return {
        "package_id": str(metadata.package_id),
        "package_hash": metadata.package_hash,
        "consumer_type": metadata.consumer_type.value,
        "consumer_id": str(metadata.consumer_id),
        "context_version": metadata.context_version,
        "schema_version": materialization.schema_version,
    }


def _result_inputs(
    materialization: ContextMaterialization | None,
) -> list[dict[str, object]]:
    if isinstance(
        materialization, (ContextMaterializationV2, ContextMaterializationV3)
    ):
        return [item.to_mapping() for item in materialization.result_inputs]
    return []


def _is_rework_context(materialization: ContextMaterialization | None) -> bool:
    return isinstance(materialization, ContextMaterializationV3) and (
        materialization.code_input_manifest is not None
        and not materialization.result_inputs
    )


def _workspace_manifest_record(
    manifest: ExecutionWorkspaceManifest,
) -> dict[str, object]:
    checkout = manifest.checkouts[0]
    return {
        "manifest_id": str(manifest.id),
        "workspace_id": str(manifest.workspace_id),
        "task_id": str(manifest.task_id),
        "execution_id": str(manifest.execution_id),
        "repository_ref": checkout.repository_ref,
        "base_revision": checkout.base_revision,
        "checkout": {
            "repository_ref": checkout.repository_ref,
            "base_revision": checkout.base_revision,
            "branch": checkout.branch,
            "current_head": checkout.current_head,
        },
        "location_ref": manifest.location_ref,
        "lifecycle": manifest.lifecycle.value,
        "retention_seconds": manifest.retention_seconds,
        "retain_until": (
            manifest.retain_until.isoformat()
            if manifest.retain_until is not None
            else None
        ),
        "fencing_token": manifest.fencing_token,
        "created_at": manifest.created_at.isoformat(),
        "frozen_at": manifest.frozen_at.isoformat() if manifest.frozen_at else None,
        "released_at": manifest.released_at.isoformat()
        if manifest.released_at
        else None,
        "version": manifest.version,
    }


def _workspace_runtime_head(
    spec: ExecutionSpec,
    workspace: ExecutionWorkspaceManifest,
) -> str:
    """Return the exact Git HEAD authorized for this execution session."""

    checkout = workspace.checkouts[0]
    if spec.execution_purpose != "code":
        return checkout.current_head
    acknowledgement = spec.physical_input_ack
    if not isinstance(acknowledgement, Mapping):
        raise DemoWorkerAdapterError("code input acknowledgement is missing")
    raw_head = acknowledgement.get("actual_commit")
    if not isinstance(raw_head, str) or _GIT_OID_PATTERN.fullmatch(raw_head.strip()) is None:
        raise DemoWorkerAdapterError("code input acknowledgement has no valid commit")
    return raw_head.strip().lower()


def _allowed_repository_refs(manifest: DemoManifest) -> frozenset[str]:
    repository = manifest.repository.resolve(strict=False)
    return frozenset(
        {
            f"repo://interview/{manifest.run_id}",
            str(repository),
            opaque_repository_ref(repository),
        }
    )


def _worker_item_key(
    manifest: DemoManifest,
    worker_id: UUID,
    *,
    goal: str | None = None,
    request_purpose: str | None = None,
) -> str:
    normalized_goal = goal.casefold() if isinstance(goal, str) else ""
    normalized_purpose = (
        request_purpose.casefold() if isinstance(request_purpose, str) else ""
    )
    for marker, closure_key in (
        ("source-a.json", "source-a"),
        ("source-b.json", "source-b"),
        ("summary.json", "integration"),
    ):
        if marker in normalized_purpose or marker in normalized_goal:
            return closure_key
    keys = {
        "validation": "validate",
        "report": "report",
        "review": "review-report",
        "integration": "integrate",
    }
    if manifest.scenario == "revision":
        keys["replacement"] = "parse-tsv"
    else:
        # The spare node is a normal validation capacity in scenarios without
        # a plan-level replacement. Handoff still adds its own target fencing.
        keys["replacement"] = "validate"
    matches = [
        item_key
        for manifest_key, item_key in keys.items()
        if manifest.worker_ids[manifest_key] == worker_id
    ]
    if len(matches) != 1:
        raise DemoWorkerAdapterError("worker identity is not supported by demo steps")
    return matches[0]


def _has_worker_step_state(session: Mapping[str, object]) -> bool:
    return (
        session.get("context_identity") is not None
        and isinstance(session.get("worker_item_key"), str)
        and isinstance(session.get("workspace_path"), str)
        and isinstance(session.get("workspace_manifest"), dict)
        and isinstance(session.get("start_operation_id"), str)
    )


def _is_handoff_target(manifest: DemoManifest, spec: ExecutionSpec) -> bool:
    return manifest.scenario in {"handoff", "recovery", "controls"} and spec.worker_id == manifest.worker_ids[
        "replacement"
    ]


def _target_context_binding(
    spec: ExecutionSpec,
    *,
    session_id: UUID,
    operation_id: UUID,
) -> SessionContextBinding:
    materialization = spec.context_package
    if materialization is None:
        raise DemoWorkerAdapterError("handoff target context package is missing")
    metadata = materialization.metadata
    return SessionContextBinding(
        operation_id=operation_id,
        execution_session_id=session_id,
        consumer_execution_id=spec.execution_id,
        package_id=metadata.package_id,
        package_hash=metadata.package_hash,
        context_version=metadata.context_version or 0,
        acknowledged_at=datetime.now(UTC),
        schema_version=materialization.schema_version,
    )


def _handoff_instruction(
    materialization: ContextMaterialization | None,
) -> str | None:
    if materialization is None:
        return None
    raw_facts = materialization.control_plane_facts.facts
    if not isinstance(raw_facts, Mapping):
        return None
    handoff = raw_facts.get("handoff")
    if not isinstance(handoff, Mapping):
        return None
    instruction = handoff.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return None
    return instruction.strip()


def _execution_id_from_session(handle: AdapterSessionHandle) -> UUID:
    prefix = "interview-demo-session-"
    if not handle.adapter_session_id.startswith(prefix):
        raise DemoWorkerAdapterError("adapter session identity is invalid")
    try:
        return UUID(handle.adapter_session_id.removeprefix(prefix))
    except ValueError as exc:
        raise DemoWorkerAdapterError("adapter session identity is invalid") from exc


def _delivery_from_record(
    record: OperationRecord,
    operation_id: UUID,
    session_id: UUID,
) -> DeliveryResult:
    try:
        sequence = record.result["accepted_sequence"]
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoWorkerAdapterError("persisted stop result is corrupt") from exc
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise DemoWorkerAdapterError("persisted stop result is corrupt")
    return DeliveryResult(
        operation_id=operation_id,
        outcome=DeliveryOutcome.DELIVERED,
        accepted_sequence=sequence,
        failure_code=None,
        execution_session_id=session_id,
    )
