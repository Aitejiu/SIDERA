from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
    ContextMaterializationV1,
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
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffInspectionOutcome,
    HandoffOperationInspection,
    SessionContextBinding,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    HandoffCheckpointMode,
    IdempotencyCapability,
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


class FakeAdapterError(RuntimeError):
    """A deterministic failure injected into the fake worker adapter."""


class FakeWorkerAdapter:
    """In-memory WorkerAdapter used for deterministic control-plane tests."""

    _CAPABILITIES = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
        context_patch_versions=(1,),
        handoff_checkpoint_mode=HandoffCheckpointMode.NATIVE,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
    )

    @classmethod
    def capabilities(cls) -> WorkerAdapterCapabilityProfile:
        return cls._CAPABILITIES

    def __init__(
        self,
        *,
        artifacts: tuple[ArtifactCandidate, ...] = (),
        knowledge_candidates: tuple[KnowledgeCandidateProposalV1, ...] = (),
        failures: Mapping[str, str] | None = None,
        handoff_context: tuple[UUID, str, int] | None = None,
        handoff_workspace_digest: str | None = None,
    ) -> None:
        self._artifacts = tuple(artifacts)
        if any(
            not isinstance(item, KnowledgeCandidateProposalV1)
            for item in knowledge_candidates
        ):
            raise TypeError("knowledge_candidates must be typed proposals")
        self._knowledge_candidates = tuple(knowledge_candidates)
        self._failures = dict(failures or {})
        if handoff_context is not None:
            package_id, package_hash, context_version = handoff_context
            if not isinstance(package_id, UUID) or not isinstance(package_hash, str):
                raise TypeError("handoff_context must contain a UUID and hash")
            if context_version < 1:
                raise ValueError("handoff context version must be positive")
            self._handoff_context = (package_id, package_hash, context_version)
        else:
            self._handoff_context = (
                uuid5(NAMESPACE_URL, "sidera.fake.handoff.package"),
                "0" * 64,
                1,
            )
        if handoff_workspace_digest is not None and len(handoff_workspace_digest) != 64:
            raise ValueError("handoff workspace digest must be a SHA-256 hash")
        self._handoff_workspace_digest = handoff_workspace_digest
        self._checkpoint_override: tuple[bool, str | None] | None = None
        self._patch_outcomes: deque[tuple[DeliveryOutcome, str | None]] = deque()
        self._patch_results: dict[UUID, ContextPatchDeliveryResult] = {}
        self._applied_context_patches: list[ContextPatchMaterializationV1] = []
        self._sessions: dict[str, _FakeSession] = {}
        self._handoff_operations: dict[UUID, _FakeHandoffOperation] = {}
        self._handoff_resume_count = 0
        self._handoff_resume_instructions: list[str] = []
        self._checkpoint_call_count = 0
        self._inspect_call_count = 0
        self._lock = RLock()

    def probe(self, config: Mapping[str, object]) -> WorkerAdapterCapabilityProfile:
        del config
        self._maybe_fail("probe")
        return self._CAPABILITIES

    def accept_context(
        self,
        materialization: ContextMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        _validate_context(materialization, operation_id)
        metadata = materialization.metadata
        return ContextAcknowledgement(
            package_id=metadata.package_id,
            package_hash=metadata.package_hash,
            consumer_type=metadata.consumer_type,
            consumer_id=metadata.consumer_id,
            context_version=metadata.context_version,
            adapter_module_id=uuid5(
                NAMESPACE_URL, "sidera.fake.worker.context-adapter"
            ),
            adapter_config_version=1,
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
        self._maybe_fail("start_session")
        now = datetime.now(UTC)
        execution_session_id = uuid5(
            NAMESPACE_URL, f"sidera.fake.execution-session:{spec.execution_id}"
        )
        adapter_session_id = f"fake-session-{spec.execution_id}"
        handoff_facts = _handoff_facts(getattr(spec, "context_package", None))
        context_binding = None
        handoff_instruction = None
        if handoff_facts is not None:
            context = getattr(spec, "context_package", None)
            assert context is not None
            metadata = context.metadata
            context_binding = SessionContextBinding(
                operation_id=operation_id,
                execution_session_id=execution_session_id,
                consumer_execution_id=spec.execution_id,
                package_id=metadata.package_id,
                package_hash=metadata.package_hash,
                context_version=metadata.context_version or 0,
                acknowledged_at=now,
            )
            handoff_instruction = _handoff_instruction(handoff_facts)
        handle = AdapterSessionHandle(
            execution_session_id=execution_session_id,
            adapter_session_id=adapter_session_id,
            process_ref=f"fake://{spec.execution_id}",
            capability_snapshot=self._CAPABILITIES,
            last_accepted_sequence=0,
            started_at=now,
            context_binding=context_binding,
        )
        with self._lock:
            self._sessions[adapter_session_id] = _FakeSession(
                handle=handle,
                events=deque(
                    [
                        AdapterEvent(
                            event_type="session.started",
                            sequence=1,
                            payload={"execution_id": str(spec.execution_id)},
                            occurred_at=now,
                        )
                    ]
                ),
                next_sequence=2,
                execution_id=spec.execution_id,
                workspace_digest=(
                    self._handoff_workspace_digest
                    or workspace_manifest_digest(workspace)
                ),
                handoff_context=self._handoff_context,
                handoff_held=handoff_facts is not None,
                handoff_instruction=handoff_instruction,
                handoff_fencing_token=getattr(spec, "fencing_token", 1),
            )
        return handle

    def read_events(self, handle: AdapterSessionHandle) -> Iterable[AdapterEvent]:
        self._maybe_fail("read_events")
        session = self._session(handle)
        with self._lock:
            events = tuple(session.events)
            session.events.clear()
        return events

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        del attachment_refs
        self._maybe_fail("send_input")
        if not text.strip():
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.FAILED,
                accepted_sequence=None,
                failure_code="empty_input",
            )
        session = self._session(handle)
        with self._lock:
            if session.stopped:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.FAILED,
                    accepted_sequence=None,
                    failure_code="session_stopped",
                )
            if session.handoff_held and not session.writer_enabled:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="handoff_writer_disabled",
                )
            if session.checkpointed:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="handoff_checkpointed",
                )
            if session.prompt_busy:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="prompt_busy",
                )
            sequence = session.next_sequence
            session.next_sequence += 1
            session.last_accepted_sequence = sequence
            session.prompt_busy = True
            session.events.append(
                AdapterEvent(
                    event_type="input.accepted",
                    sequence=sequence,
                    payload={"text_length": len(text)},
                    occurred_at=datetime.now(UTC),
                )
            )
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=sequence,
                failure_code=None,
            )

    def apply_context_patch(
        self,
        handle: AdapterSessionHandle,
        materialization: ContextPatchMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextPatchDeliveryResult:
        if not isinstance(materialization, ContextPatchMaterializationV1):
            raise TypeError("materialization must be a ContextPatchMaterializationV1")
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        patch_hash = materialization.patch.patch_hash
        previous = self._patch_results.get(operation_id)
        if previous is not None:
            if previous.patch_hash != patch_hash:
                raise FakeAdapterError("patch_operation_id_reused")
            return previous
        self._maybe_fail("apply_context_patch")
        session = self._session(handle)
        with self._lock:
            if session.handle.execution_session_id != handle.execution_session_id:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="failed",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="session_identity_mismatch",
                    )
                )
            if session.handoff_held and not session.writer_enabled:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="blocked",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="handoff_writer_disabled",
                    )
                )
            if session.checkpointed:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="blocked",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="handoff_checkpointed",
                    )
                )
            if session.stopped:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="failed",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="session_stopped",
                    )
                )
            if materialization.patch.expires_at <= datetime.now(UTC):
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="failed",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="context_patch_expired",
                    )
                )
            if self._patch_outcomes:
                outcome, error_code = self._patch_outcomes.popleft()
                if outcome is not DeliveryOutcome.DELIVERED:
                    return self._remember_patch_result(
                        ContextPatchDeliveryResult(
                            outcome=outcome.value,
                            operation_id=operation_id,
                            patch_hash=patch_hash,
                            accepted_sequence=None,
                            error_code=error_code or outcome.value,
                        )
                    )
            if session.prompt_busy:
                return self._remember_patch_result(
                    ContextPatchDeliveryResult(
                        outcome="blocked",
                        operation_id=operation_id,
                        patch_hash=patch_hash,
                        accepted_sequence=None,
                        error_code="prompt_busy",
                    )
                )
            sequence = session.next_sequence
            session.next_sequence += 1
            session.last_accepted_sequence = sequence
            session.prompt_busy = True
            session.events.append(
                AdapterEvent(
                    event_type="context.patch.accepted",
                    sequence=sequence,
                    payload={"patch_hash": patch_hash},
                    occurred_at=datetime.now(UTC),
                )
            )
            self._applied_context_patches.append(materialization)
            return self._remember_patch_result(
                ContextPatchDeliveryResult(
                    outcome="delivered",
                    operation_id=operation_id,
                    patch_hash=patch_hash,
                    accepted_sequence=sequence,
                    error_code=None,
                )
            )

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        self._maybe_fail("stop_session")
        session = self._session(handle)
        with self._lock:
            previous = self._handoff_operations.get(operation_id)
            if previous is not None:
                if previous.operation_type is not GatewayOperationType.STOP_SESSION:
                    raise FakeAdapterError("operation_id_reused")
                if not isinstance(previous.result, DeliveryResult):
                    raise FakeAdapterError("operation_result_mismatch")
                return previous.result
            if session.stopped:
                if (
                    session.expected_stop_operation_id is not None
                    and operation_id != session.expected_stop_operation_id
                ):
                    raise FakeAdapterError("handoff_stop_identity_mismatch")
                result = DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=session.last_accepted_sequence,
                    failure_code=None,
                    execution_session_id=session.handle.execution_session_id,
                )
                self._remember_handoff_operation(
                    operation_id, GatewayOperationType.STOP_SESSION, result
                )
                return result
            if session.checkpointed:
                if operation_id != session.expected_stop_operation_id:
                    raise FakeAdapterError("handoff_stop_identity_mismatch")
                self._maybe_fail("handoff_stop_before")
                session.stopped = True
                session.prompt_busy = False
                sequence = session.next_sequence
                session.next_sequence += 1
                session.events.append(
                    AdapterEvent(
                        event_type="session.canceled",
                        sequence=sequence,
                        payload={"handoff": True},
                        occurred_at=datetime.now(UTC),
                    )
                )
                result = DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=session.checkpoint_sequence,
                    failure_code=None,
                    execution_session_id=session.handle.execution_session_id,
                )
                self._remember_handoff_operation(
                    operation_id, GatewayOperationType.STOP_SESSION, result
                )
                self._maybe_fail("handoff_stop_after")
                return result
            session.stopped = True
            session.prompt_busy = False
            sequence = session.next_sequence
            session.next_sequence += 1
            session.events.append(
                AdapterEvent(
                    event_type="session.canceled",
                    sequence=sequence,
                    payload={},
                    occurred_at=datetime.now(UTC),
                )
            )
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=sequence,
                failure_code=None,
                execution_session_id=session.handle.execution_session_id,
            )

    def collect_artifacts(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[ArtifactCandidate, ...]:
        del operation_id
        self._maybe_fail("collect_artifacts")
        self._session(handle)
        return self._artifacts

    def collect_knowledge_candidates(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[KnowledgeCandidateProposalV1, ...]:
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        self._maybe_fail("collect_knowledge_candidates")
        self._session(handle)
        return self._knowledge_candidates

    def capture_handoff_checkpoint(
        self,
        handle: AdapterSessionHandle,
        *,
        transition_id: UUID,
        source_input_cutoff_sequence: int,
        operation_id: UUID,
    ) -> HandoffCheckpointEvidence:
        if not isinstance(transition_id, UUID) or not isinstance(operation_id, UUID):
            raise TypeError("handoff checkpoint identifiers must be UUIDs")
        if source_input_cutoff_sequence < 0:
            raise ValueError("source input cutoff must be non-negative")
        session = self._session(handle)
        with self._lock:
            previous = self._handoff_operations.get(operation_id)
            if previous is not None:
                if previous.operation_type is not GatewayOperationType.HANDOFF_CHECKPOINT:
                    raise FakeAdapterError("operation_id_reused")
                if not isinstance(previous.result, HandoffCheckpointEvidence):
                    raise FakeAdapterError("checkpoint_result_mismatch")
                return previous.result
            self._maybe_fail("handoff_checkpoint_before")
            self._maybe_fail("capture_handoff_checkpoint")
            if session.stopped:
                raise FakeAdapterError("session_stopped")
            if session.checkpointed:
                raise FakeAdapterError("handoff_checkpointed")
            if session.prompt_busy:
                raise FakeAdapterError("prompt_busy")
            if session.last_accepted_sequence < source_input_cutoff_sequence:
                raise FakeAdapterError("handoff_checkpoint_sequence_stale")
            self._checkpoint_call_count += 1
            session.events.clear()
            accepted_sequence = session.last_accepted_sequence
            package_id, package_hash, context_version = session.handoff_context
            override = self._checkpoint_override
            self._checkpoint_override = None
            if override is not None and not override[0]:
                raise ValueError("checkpoint evidence must be quiescent")
            evidence = HandoffCheckpointEvidence(
                operation_id=operation_id,
                transition_id=transition_id,
                source_session_id=session.handle.execution_session_id,
                accepted_sequence=accepted_sequence,
                workspace_digest=override[1] if override and override[1] else session.workspace_digest,
                context_package_id=package_id,
                context_package_hash=package_hash,
                context_version=context_version,
                progress_summary_ref=(
                    f"fake-progress://{session.execution_id}/{accepted_sequence}"
                ),
                artifact_refs=(f"fake-artifact://{session.execution_id}",),
                evidence_refs=(f"fake-evidence://{session.execution_id}",),
                quiescent=True,
                produced_at=datetime.now(UTC),
            )
            session.checkpointed = True
            session.checkpoint_sequence = accepted_sequence
            session.expected_stop_operation_id = uuid5(
                NAMESPACE_URL,
                f"sidera.gateway:stop_session:{session.execution_id}",
            )
            self._remember_handoff_operation(
                operation_id, GatewayOperationType.HANDOFF_CHECKPOINT, evidence
            )
            self._maybe_fail("handoff_checkpoint_after")
            return evidence

    def inspect_handoff_operation(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
    ) -> HandoffOperationInspection:
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        self._inspect_call_count += 1
        with self._lock:
            record = self._handoff_operations.get(operation_id)
            if record is None:
                session = next(
                    (
                        item
                        for item in self._sessions.values()
                        if item.handle.execution_session_id == handle.execution_session_id
                    ),
                    None,
                )
                return HandoffOperationInspection(
                    operation_id=operation_id,
                    operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
                    outcome=HandoffInspectionOutcome.ABSENT,
                    execution_id=session.execution_id if session is not None else None,
                    session_id=handle.execution_session_id,
                    accepted_sequence=None,
                    context_binding=handle.context_binding,
                    observed_at=datetime.now(UTC),
                    evidence_refs=(),
                )
            operation_type, result = record.operation_type, record.result
            accepted_sequence = getattr(result, "accepted_sequence", None)
            return HandoffOperationInspection(
                operation_id=operation_id,
                operation_type=operation_type,
                outcome=HandoffInspectionOutcome.SUCCEEDED,
                execution_id=next(
                    item.execution_id
                    for item in self._sessions.values()
                    if item.handle.execution_session_id == handle.execution_session_id
                ),
                session_id=handle.execution_session_id,
                accepted_sequence=accepted_sequence,
                context_binding=handle.context_binding,
                observed_at=datetime.now(UTC),
                evidence_refs=(f"fake-inspection://{operation_id}",),
            )

    def enable_handoff_writer(
        self,
        handle: AdapterSessionHandle,
        *,
        fencing_token: int,
        operation_id: UUID,
    ) -> DeliveryResult:
        if not isinstance(fencing_token, int) or fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        with self._lock:
            previous = self._handoff_operations.get(operation_id)
            if previous is not None:
                if previous.operation_type is not GatewayOperationType.HANDOFF_ENABLE_WRITER:
                    raise FakeAdapterError("operation_id_reused")
                if not isinstance(previous.result, DeliveryResult):
                    raise FakeAdapterError("writer_result_mismatch")
                return previous.result
            session = self._session(handle)
            if session.handle.execution_session_id != handle.execution_session_id:
                raise FakeAdapterError("session_identity_mismatch")
            if handle.context_binding is None or not session.handoff_held:
                raise FakeAdapterError("handoff_context_binding_missing")
            if fencing_token != session.handoff_fencing_token:
                raise FakeAdapterError("handoff_fencing_mismatch")
            if session.stopped:
                raise FakeAdapterError("session_stopped")
            if session.writer_enabled:
                raise FakeAdapterError("handoff_writer_operation_missing")
            self._maybe_fail("handoff_enable_writer_before")
            session.writer_enabled = True
            sequence = session.next_sequence
            session.next_sequence += 1
            session.last_accepted_sequence = sequence
            session.events.append(
                AdapterEvent(
                    event_type="handoff.writer_enabled",
                    sequence=sequence,
                    payload={
                        "context_version": handle.context_binding.context_version,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            self._handoff_resume_count += 1
            self._handoff_resume_instructions.append(
                session.handoff_instruction
                or "continue from the transferred context"
            )
            result = DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=sequence,
                failure_code=None,
                execution_session_id=session.handle.execution_session_id,
            )
            self._maybe_fail("handoff_enable_writer_after")
            self._remember_handoff_operation(
                operation_id, GatewayOperationType.HANDOFF_ENABLE_WRITER, result
            )
            return result

    def release_boundary(self, handle: AdapterSessionHandle) -> None:
        """Make the current prompt safe for the next queued input."""

        session = self._session(handle)
        with self._lock:
            if session.stopped:
                raise FakeAdapterError("session_stopped")
            if session.handoff_held and not session.writer_enabled:
                raise FakeAdapterError("handoff_writer_disabled")
            if session.checkpointed:
                raise FakeAdapterError("handoff_checkpointed")
            session.prompt_busy = False
            sequence = session.next_sequence
            session.next_sequence += 1
            session.events.append(
                AdapterEvent(
                    event_type="checkpoint.created",
                    sequence=sequence,
                    payload={"boundary": "prompt_complete"},
                    occurred_at=datetime.now(UTC),
                )
            )

    def active_sessions(self) -> tuple[AdapterSessionHandle, ...]:
        with self._lock:
            return tuple(
                session.handle
                for session in self._sessions.values()
                if not session.stopped
            )

    @property
    def checkpoint_call_count(self) -> int:
        return self._checkpoint_call_count

    @property
    def inspect_call_count(self) -> int:
        return self._inspect_call_count

    @property
    def handoff_resume_count(self) -> int:
        return self._handoff_resume_count

    @property
    def handoff_resume_instructions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._handoff_resume_instructions)

    def pending_events(self, handle: AdapterSessionHandle) -> tuple[AdapterEvent, ...]:
        session = self._session(handle)
        with self._lock:
            return tuple(session.events)

    def inject_handoff_checkpoint(
        self,
        *,
        quiescent: bool = True,
        workspace_digest: str | None = None,
    ) -> None:
        if not isinstance(quiescent, bool):
            raise TypeError("quiescent must be a boolean")
        if workspace_digest is not None and len(workspace_digest) != 64:
            raise ValueError("handoff workspace digest must be a SHA-256 hash")
        self._checkpoint_override = (quiescent, workspace_digest)

    @property
    def applied_context_patches(self) -> tuple[ContextPatchMaterializationV1, ...]:
        with self._lock:
            return tuple(self._applied_context_patches)

    def inject_patch_outcome(
        self,
        outcome: DeliveryOutcome | str,
        *,
        error_code: str | None = None,
    ) -> None:
        try:
            normalized = (
                outcome
                if isinstance(outcome, DeliveryOutcome)
                else DeliveryOutcome(outcome)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported patch outcome: {outcome!r}") from exc
        if normalized is DeliveryOutcome.DELIVERED and error_code is not None:
            raise ValueError("delivered patch outcome cannot have an error_code")
        if error_code is not None and not error_code.strip():
            raise ValueError("error_code is required when provided")
        self._patch_outcomes.append((normalized, error_code))

    def inject_failure(self, operation: str, failure_code: str) -> None:
        clean_code = failure_code.strip()
        if not clean_code:
            raise ValueError("failure_code is required")
        self._failures[operation] = clean_code

    def _session(self, handle: AdapterSessionHandle) -> "_FakeSession":
        try:
            return self._sessions[handle.adapter_session_id]
        except KeyError as exc:
            raise FakeAdapterError("unknown_session") from exc

    def _maybe_fail(self, operation: str) -> None:
        failure_code = self._failures.get(operation)
        if failure_code is not None:
            raise FakeAdapterError(failure_code)

    def _remember_patch_result(
        self, result: ContextPatchDeliveryResult
    ) -> ContextPatchDeliveryResult:
        self._patch_results[result.operation_id] = result
        return result

    def _remember_handoff_operation(
        self,
        operation_id: UUID,
        operation_type: GatewayOperationType,
        result: HandoffCheckpointEvidence | DeliveryResult,
    ) -> None:
        self._handoff_operations[operation_id] = _FakeHandoffOperation(
            operation_type=operation_type,
            result=result,
        )


@dataclass(frozen=True, slots=True)
class _FakeHandoffOperation:
    operation_type: GatewayOperationType
    result: HandoffCheckpointEvidence | DeliveryResult


class _FakeSession:
    def __init__(
        self,
        *,
        handle: AdapterSessionHandle,
        events: deque[AdapterEvent],
        next_sequence: int,
        execution_id: UUID,
        workspace_digest: str,
        handoff_context: tuple[UUID, str, int],
        handoff_held: bool,
        handoff_instruction: str | None,
        handoff_fencing_token: int,
    ) -> None:
        self.handle = handle
        self.events = events
        self.next_sequence = next_sequence
        self.execution_id = execution_id
        self.workspace_digest = workspace_digest
        self.handoff_context = handoff_context
        self.handoff_held = handoff_held
        self.handoff_instruction = handoff_instruction
        self.handoff_fencing_token = handoff_fencing_token
        self.writer_enabled = not handoff_held
        self.last_accepted_sequence = 0
        self.checkpointed = False
        self.checkpoint_sequence = 0
        self.expected_stop_operation_id: UUID | None = None
        self.prompt_busy = False
        self.stopped = False


def _handoff_facts(
    materialization: ContextMaterializationV1 | None,
) -> Mapping[str, object] | None:
    if materialization is None:
        return None
    facts = materialization.control_plane_facts.facts
    handoff = facts.get("handoff")
    return handoff if isinstance(handoff, Mapping) else None


def _handoff_instruction(facts: Mapping[str, object]) -> str:
    instruction = facts.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("handoff context is missing its resume instruction")
    return instruction.strip()


def _validate_context(
    materialization: ContextMaterializationV1,
    operation_id: UUID,
) -> None:
    if not isinstance(materialization, ContextMaterializationV1):
        raise TypeError("materialization must be a ContextMaterializationV1")
    if not isinstance(operation_id, UUID):
        raise TypeError("operation_id must be a UUID")
    if materialization.schema_version != 1:
        raise ValueError("unsupported context materialization schema")
    if (
        materialization.metadata.purpose is not ContextPurpose.EXECUTION
        or materialization.metadata.consumer_type is not ContextConsumerType.WORKER
    ):
        raise ValueError("worker adapter requires an execution worker context")
    if materialization.total_byte_count > 256 * 1024:
        raise ValueError("context materialization exceeds adapter size limit")
