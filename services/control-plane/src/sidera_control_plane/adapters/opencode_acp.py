import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from subprocess import Popen
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.adapters.acp_process import (
    ACPProcessError as OpenCodeAdapterError,
)
from sidera_control_plane.adapters.acp_process import (
    ACPProcessProtocolError as ACPProtocolError,
)
from sidera_control_plane.adapters.acp_process import (
    ACPProcessTimeoutError as ACPTimeoutError,
)
from sidera_control_plane.adapters.acp_process import (
    ACPProcessTransport,
)
from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
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
    ProcessDescriptor,
    workspace_manifest_digest,
)


class AdapterProbeError(OpenCodeAdapterError):
    """The configured OpenCode process is unavailable or incompatible."""


class OpenCodeHandoffUnsupported(NotImplementedError):
    """The installed OpenCode ACP process cannot prove the handoff boundary."""


def handoff_profile_supported(profile: WorkerAdapterCapabilityProfile) -> bool:
    """Return whether a profile proves every R6.2 handoff capability."""

    return (
        profile.handoff_checkpoint_mode is HandoffCheckpointMode.CONTROL_EVENT
        and 1 in profile.handoff_checkpoint_versions
        and 1 in profile.handoff_session_context_versions
        and 1 in profile.handoff_writer_gate_versions
        and profile.handoff_inspection
        and profile.stop is StopCapability.EXACT_EXECUTION
    )


class OpenCodeACPAdapter:
    """WorkerAdapter implementation for one OpenCode ACP child process per session."""

    _PROFILE = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
        context_package_versions=(1, 2),
        context_patch_versions=(),
    )
    _HANDOFF_PROFILE = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
        context_package_versions=(1, 2),
        context_patch_versions=(),
        handoff_checkpoint_mode=HandoffCheckpointMode.CONTROL_EVENT,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
    )

    def __init__(
        self,
        *,
        executable: str = "opencode",
        model: str | None = None,
        workspace_root: Path | None = None,
        request_timeout_seconds: float = 30.0,
        handoff_request_timeout_seconds: float | None = None,
        stop_grace_seconds: float = 5.0,
        git_executable: str = "git",
        stderr_limit_bytes: int = 64 * 1024,
        client_name: str = "sidera-control-plane",
        client_version: str = "0.1.0",
        context_package_versions: tuple[int, ...] = (1, 2),
    ) -> None:
        executable = executable.strip()
        if not executable:
            raise ValueError("OpenCode executable is required")
        if request_timeout_seconds <= 0 or stop_grace_seconds <= 0:
            raise ValueError("OpenCode timeouts must be positive")
        if (
            handoff_request_timeout_seconds is not None
            and handoff_request_timeout_seconds <= 0
        ):
            raise ValueError("OpenCode handoff timeout must be positive")
        if stderr_limit_bytes <= 0:
            raise ValueError("stderr limit must be positive")
        if not git_executable.strip():
            raise ValueError("Git executable is required")
        declared_context_versions = _declared_context_package_versions(
            context_package_versions
        )
        self._executable = executable
        self._model = model.strip() if model is not None and model.strip() else None
        self._workspace_root = (
            workspace_root.expanduser().resolve()
            if workspace_root is not None
            else None
        )
        self._request_timeout_seconds = request_timeout_seconds
        self._handoff_request_timeout_seconds = (
            handoff_request_timeout_seconds or request_timeout_seconds
        )
        self._stop_grace_seconds = stop_grace_seconds
        self._git_executable = git_executable.strip()
        self._stderr_limit_bytes = stderr_limit_bytes
        self._client_name = client_name
        self._client_version = client_version
        self._sessions: dict[str, _OpenCodeSession] = {}
        self._profile = replace(
            self._PROFILE,
            context_package_versions=declared_context_versions,
        )
        self._handoff_profile = replace(
            self._HANDOFF_PROFILE,
            context_package_versions=declared_context_versions,
        )
        self._lock = threading.RLock()

    def probe(self, config: Mapping[str, object]) -> WorkerAdapterCapabilityProfile:
        executable = _config_text(config, "executable", self._executable)
        cwd = _config_path(config, "cwd", Path.cwd())
        transport = self._spawn(executable, cwd, probe=True)
        try:
            initialize = transport.request(
                "initialize",
                self._initialize_params(config),
                timeout=self._request_timeout_seconds,
            )
            self._validate_initialize(initialize)
            session = transport.request(
                "session/new",
                {"cwd": str(cwd), "mcpServers": _config_mcp_servers(config)},
                timeout=self._request_timeout_seconds,
            )
            _session_id = _session(session)
            handoff_supported = self._try_negotiate_handoff_modes(
                transport, _session_id, session, keep_plan=False
            )
            self._profile = (
                self._handoff_profile if handoff_supported else self._profile
            )
            return self._profile
        except ACPTimeoutError:
            raise
        except (ACPProtocolError, OpenCodeAdapterError) as exc:
            raise AdapterProbeError(f"OpenCode ACP handshake failed: {exc}") from exc
        finally:
            transport.terminate(grace_seconds=self._stop_grace_seconds)
            transport.close()

    def accept_context(
        self,
        materialization: ContextMaterialization,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        _validate_context(materialization, operation_id)
        if not self._profile.supports_context_package_version(
            materialization.schema_version
        ):
            raise ValueError(
                f"materialization schema v{materialization.schema_version} is not "
                "declared by the OpenCode adapter capability profile"
            )
        metadata = materialization.metadata
        return ContextAcknowledgement(
            package_id=metadata.package_id,
            package_hash=metadata.package_hash,
            consumer_type=metadata.consumer_type,
            consumer_id=metadata.consumer_id,
            context_version=metadata.context_version,
            adapter_module_id=uuid5(
                NAMESPACE_URL, "sidera.opencode.worker.context-adapter"
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
        cwd = self._workspace_path(workspace)
        transport = self._spawn(self._executable, cwd, probe=False)
        try:
            initialize = transport.request(
                "initialize",
                self._initialize_params({}),
                timeout=self._request_timeout_seconds,
            )
            self._validate_initialize(initialize)
            result = transport.request(
                "session/new",
                {"cwd": str(cwd), "mcpServers": []},
                timeout=self._request_timeout_seconds,
            )
            adapter_session_id = _session(result)
            execution_session_id = uuid4()
            handoff_facts = _handoff_facts(spec.context_package)
            handoff_instruction = (
                _handoff_instruction(handoff_facts)
                if handoff_facts is not None
                else None
            )
            handoff_supported = self._try_negotiate_handoff_modes(
                transport,
                adapter_session_id,
                result,
                keep_plan=handoff_facts is not None,
            )
            if handoff_facts is not None and not handoff_supported:
                raise OpenCodeHandoffUnsupported("opencode_handoff_unsupported")
            identity = _process_identity(transport.process)
            context_binding = None
            if spec.context_package is not None and handoff_facts is not None:
                metadata = spec.context_package.metadata
                context_binding = SessionContextBinding(
                    operation_id=operation_id,
                    execution_session_id=execution_session_id,
                    consumer_execution_id=spec.execution_id,
                    package_id=metadata.package_id,
                    package_hash=metadata.package_hash,
                    context_version=metadata.context_version or 0,
                    acknowledged_at=datetime.now(UTC),
                )
            profile = (
                self._handoff_profile if handoff_supported else self._profile
            )
            handle = AdapterSessionHandle(
                execution_session_id=execution_session_id,
                adapter_session_id=adapter_session_id,
                process_ref=f"opencode://{identity.nonce}",
                capability_snapshot=profile,
                last_accepted_sequence=0,
                started_at=identity.started_at,
                context_binding=context_binding,
            )
            with self._lock:
                self._sessions[adapter_session_id] = _OpenCodeSession(
                    handle=handle,
                    transport=transport,
                    identity=identity,
                    cwd=cwd,
                    acceptance_criteria=spec.acceptance_criteria,
                    execution_id=spec.execution_id,
                    fencing_token=spec.fencing_token,
                    workspace_digest=workspace_manifest_digest(workspace),
                    context_package=spec.context_package,
                    context_binding=context_binding,
                    handoff_instruction=handoff_instruction,
                    handoff_held=handoff_facts is not None,
                    handoff_supported=handoff_supported,
                )
            return handle
        except Exception:
            transport.terminate(grace_seconds=self._stop_grace_seconds)
            transport.close()
            raise

    def read_events(self, handle: AdapterSessionHandle) -> tuple[AdapterEvent, ...]:
        session = self._session(handle)
        with session.lock:
            if not session.stopped:
                session.transport.read_available(timeout=0)
            self._append_notifications(session)
            events = tuple(session.events)
            session.events.clear()
            terminal = self._observe_terminal_locked(session)
            if terminal is not None:
                events = (*events, terminal)
            return events

    def observe_terminal(
        self, handle: AdapterSessionHandle
    ) -> AdapterEvent | None:
        """Return one durable terminal observation when the ACP process exits."""
        session = self._session(handle)
        with session.lock:
            return self._observe_terminal_locked(session)

    def send_input(
        self,
        handle: AdapterSessionHandle,
        text: str,
        attachment_refs: tuple[str, ...],
        *,
        operation_id: UUID,
    ) -> DeliveryResult:
        if not text.strip():
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.FAILED,
                accepted_sequence=None,
                failure_code="empty_input",
            )
        session = self._session(handle)
        with session.lock:
            if session.handle.execution_session_id != handle.execution_session_id:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="session_identity_mismatch",
                    execution_session_id=session.handle.execution_session_id,
                )
            if session.stopped or session.transport.process.poll() is not None:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="process_identity_unknown",
                    execution_session_id=session.handle.execution_session_id,
                )
            if session.handoff_state_unknown:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="handoff_state_unknown",
                    execution_session_id=session.handle.execution_session_id,
                )
            if session.checkpointed:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="handoff_checkpointed",
                    execution_session_id=session.handle.execution_session_id,
                )
            if session.handoff_held and not session.writer_enabled:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="handoff_writer_disabled",
                    execution_session_id=session.handle.execution_session_id,
                )
            if session.prompt_busy:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code="prompt_busy",
                    execution_session_id=session.handle.execution_session_id,
                )
            session.prompt_busy = True
            try:
                result = session.transport.request(
                    "session/prompt",
                    {
                        "sessionId": handle.adapter_session_id,
                        "prompt": _prompt_content(text, attachment_refs),
                    },
                    timeout=self._request_timeout_seconds,
                )
                stop_reason = (
                    result.get("stopReason") if isinstance(result, Mapping) else None
                )
                if stop_reason == "cancelled":
                    return DeliveryResult(
                        operation_id=operation_id,
                        outcome=DeliveryOutcome.FAILED,
                        accepted_sequence=None,
                        failure_code="prompt_cancelled",
                        execution_session_id=session.handle.execution_session_id,
                    )
                if stop_reason != "end_turn":
                    return DeliveryResult(
                        operation_id=operation_id,
                        outcome=DeliveryOutcome.UNKNOWN,
                        accepted_sequence=None,
                        failure_code="prompt_completion_unproven",
                        execution_session_id=session.handle.execution_session_id,
                    )
                self._append_notifications(session)
                sequence = session.next_sequence
                session.next_sequence += 1
                session.last_accepted_sequence = sequence
                session.events.append(
                    self._event(
                        session,
                        "input.accepted",
                        sequence,
                        {"text_length": len(text)},
                    )
                )
                handle = _with_last_sequence(handle, sequence)
                session.handle = handle
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=sequence,
                    failure_code=None,
                    execution_session_id=session.handle.execution_session_id,
                )
            except ACPTimeoutError:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="acp_response_timeout",
                    execution_session_id=session.handle.execution_session_id,
                )
            except ACPProtocolError as exc:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.FAILED,
                    accepted_sequence=None,
                    failure_code=_failure_code(exc),
                    execution_session_id=session.handle.execution_session_id,
                )
            finally:
                session.prompt_busy = False

    def apply_context_patch(
        self,
        handle: AdapterSessionHandle,
        materialization: ContextPatchMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextPatchDeliveryResult:
        del handle, materialization, operation_id
        raise OpenCodeAdapterError(
            "OpenCode ACP adapter does not support context patch delivery"
        )

    def stop_session(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> DeliveryResult:
        session = self._session(handle)
        with session.lock:
            if session.handle.execution_session_id != handle.execution_session_id:
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="session_identity_mismatch",
                    execution_session_id=session.handle.execution_session_id,
                )
            previous = session.handoff_operations.get(operation_id)
            if previous is not None:
                if previous.operation_type is not GatewayOperationType.STOP_SESSION:
                    raise OpenCodeAdapterError("handoff_operation_id_reused")
                if not isinstance(previous.result, DeliveryResult):
                    raise OpenCodeAdapterError("handoff_stop_result_mismatch")
                return previous.result
            if session.stopped:
                result = DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=session.last_accepted_sequence,
                    failure_code=None,
                    execution_session_id=session.handle.execution_session_id,
                )
                self._remember_handoff_operation(
                    session, operation_id, GatewayOperationType.STOP_SESSION, result
                )
                return result
            if session.expected_stop_operation_id is not None and operation_id != (
                session.expected_stop_operation_id
            ):
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="handoff_stop_identity_mismatch",
                    execution_session_id=session.handle.execution_session_id,
                )
            if not _identity_matches(session.identity):
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="process_identity_unproven",
                    execution_session_id=session.handle.execution_session_id,
                )
            session.transport.notify(
                "session/cancel", {"sessionId": handle.adapter_session_id}
            )
            if not _terminate_verified_process(
                session.transport.process,
                session.identity,
                grace_seconds=self._stop_grace_seconds,
            ):
                return DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.UNKNOWN,
                    accepted_sequence=None,
                    failure_code="process_stop_unconfirmed",
                    execution_session_id=session.handle.execution_session_id,
                )
            session.stopped = True
            if session.checkpointed:
                sequence = session.next_sequence
                session.next_sequence += 1
                accepted_sequence = session.checkpoint_sequence
            else:
                sequence = session.next_sequence
                session.next_sequence += 1
                session.last_accepted_sequence = sequence
                accepted_sequence = sequence
            session.events.append(
                self._event(session, "session.canceled", sequence, {})
            )
            session.transport.close()
            result = DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.DELIVERED,
                accepted_sequence=accepted_sequence,
                failure_code=None,
                execution_session_id=session.handle.execution_session_id,
            )
            self._remember_handoff_operation(
                session, operation_id, GatewayOperationType.STOP_SESSION, result
            )
            return result

    def collect_artifacts(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[ArtifactCandidate, ...]:
        del operation_id
        session = self._session(handle)
        with session.lock:
            if session.stopped and session.transport.process.poll() is None:
                raise OpenCodeAdapterError("stopped session has a live process")
            try:
                diff = _git_workspace_diff(self._git_executable, session.cwd)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise OpenCodeAdapterError(
                    f"OpenCode Git diff collection failed: {type(exc).__name__}: {exc}"[
                        :500
                    ]
                ) from exc
            if not diff:
                return ()
            content_hash = hashlib.sha256(diff).hexdigest()
            return (
                ArtifactCandidate(
                    artifact_type=ArtifactType.DIFF,
                    storage_ref=f"artifact://opencode/{handle.execution_session_id}/diff",
                    content_hash=content_hash,
                    size_bytes=len(diff),
                    media_type="text/x-diff",
                    provenance={
                        "producer": self._client_name,
                        "environment_ref": f"workspace://{handle.execution_session_id}",
                        "acceptance_criterion_refs": session.acceptance_criteria,
                        "conclusion": "OpenCode produced a Git diff in the execution workspace.",
                    },
                    content=diff,
                ),
            )

    def collect_knowledge_candidates(
        self, handle: AdapterSessionHandle, *, operation_id: UUID
    ) -> tuple[KnowledgeCandidateProposalV1, ...]:
        del handle
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        # ACP currently exposes no bounded structured candidate channel. Keeping
        # this hook explicit lets a future adapter add one without changing the gateway.
        return ()

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
        if not isinstance(source_input_cutoff_sequence, int) or isinstance(
            source_input_cutoff_sequence, bool
        ) or source_input_cutoff_sequence < 0:
            raise ValueError("source input cutoff must be non-negative")
        _require_handoff_profile(handle.capability_snapshot)
        session = self._session(handle)
        with session.lock:
            self._assert_session_identity(session, handle)
            previous = session.handoff_operations.get(operation_id)
            if previous is not None:
                if previous.operation_type is not GatewayOperationType.HANDOFF_CHECKPOINT:
                    raise OpenCodeAdapterError("handoff_operation_id_reused")
                if isinstance(previous.result, HandoffCheckpointEvidence):
                    return previous.result
                raise OpenCodeAdapterError("handoff_checkpoint_result_unknown")
            if session.context_package is None:
                raise OpenCodeAdapterError("handoff_context_package_missing")
            if session.stopped:
                raise OpenCodeAdapterError("session_stopped")
            if session.handoff_state_unknown:
                raise OpenCodeAdapterError("handoff_state_unknown")
            if session.checkpointed:
                raise OpenCodeAdapterError("handoff_checkpointed")
            if session.prompt_busy:
                raise OpenCodeAdapterError("prompt_busy")
            if session.last_accepted_sequence < source_input_cutoff_sequence:
                raise OpenCodeAdapterError("handoff_checkpoint_sequence_stale")
            self._remember_handoff_operation(
                session,
                operation_id,
                GatewayOperationType.HANDOFF_CHECKPOINT,
                None,
                outcome=HandoffInspectionOutcome.IN_PROGRESS,
            )
            try:
                self._set_mode(
                    session.transport,
                    session.handle.adapter_session_id,
                    "plan",
                )
                result = session.transport.request(
                    "session/prompt",
                    {
                        "sessionId": session.handle.adapter_session_id,
                        "prompt": _prompt_content(
                            _checkpoint_control_prompt(
                                operation_id=operation_id,
                                transition_id=transition_id,
                                source_input_cutoff_sequence=source_input_cutoff_sequence,
                            ),
                            (),
                        ),
                    },
                    timeout=self._handoff_request_timeout_seconds,
                )
                _require_prompt_completion(result)
                if session.transport.process.poll() is not None:
                    raise OpenCodeAdapterError("handoff_checkpoint_process_exited")
                self._append_notifications(session)
                metadata = session.context_package.metadata
                accepted_sequence = session.last_accepted_sequence
                evidence = HandoffCheckpointEvidence(
                    operation_id=operation_id,
                    transition_id=transition_id,
                    source_session_id=session.handle.execution_session_id,
                    accepted_sequence=accepted_sequence,
                    workspace_digest=session.workspace_digest,
                    context_package_id=metadata.package_id,
                    context_package_hash=metadata.package_hash,
                    context_version=metadata.context_version or 0,
                    progress_summary_ref=(
                        f"opencode-progress://{session.execution_id}/{operation_id}"
                    ),
                    artifact_refs=_context_refs(
                        session.context_package, category="artifact"
                    ),
                    evidence_refs=(
                        f"opencode-evidence://{session.execution_id}/{operation_id}",
                    ),
                    quiescent=True,
                    produced_at=datetime.now(UTC),
                )
                session.checkpointed = True
                session.checkpoint_sequence = accepted_sequence
                session.expected_stop_operation_id = gateway_operation_id(
                    session.execution_id, GatewayOperationType.STOP_SESSION
                )
                self._set_handoff_operation_result(
                    session,
                    operation_id,
                    evidence,
                    outcome=HandoffInspectionOutcome.SUCCEEDED,
                )
                session.events.append(
                    self._event(
                        session,
                        "handoff.checkpointed",
                        session.next_sequence,
                        {"accepted_sequence": accepted_sequence},
                    )
                )
                session.next_sequence += 1
                return evidence
            except Exception:
                session.handoff_state_unknown = True
                self._set_handoff_operation_outcome(
                    session, operation_id, HandoffInspectionOutcome.UNKNOWN
                )
                raise

    def inspect_handoff_operation(
        self,
        handle: AdapterSessionHandle,
        *,
        operation_id: UUID,
    ) -> HandoffOperationInspection:
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        _require_handoff_profile(handle.capability_snapshot)
        try:
            session = self._session(handle)
        except OpenCodeAdapterError:
            return _inspection(
                operation_id=operation_id,
                operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
                outcome=HandoffInspectionOutcome.UNKNOWN,
                execution_id=None,
                session_id=handle.execution_session_id,
                accepted_sequence=None,
                context_binding=None,
            )
        with session.lock:
            if session.handle.execution_session_id != handle.execution_session_id:
                return _inspection(
                    operation_id=operation_id,
                    operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
                    outcome=HandoffInspectionOutcome.UNKNOWN,
                    execution_id=session.execution_id,
                    session_id=handle.execution_session_id,
                    accepted_sequence=None,
                    context_binding=None,
                )
            record = session.handoff_operations.get(operation_id)
            if record is None:
                return _inspection(
                    operation_id=operation_id,
                    operation_type=GatewayOperationType.HANDOFF_CHECKPOINT,
                    outcome=HandoffInspectionOutcome.ABSENT,
                    execution_id=session.execution_id,
                    session_id=session.handle.execution_session_id,
                    accepted_sequence=None,
                    context_binding=session.handle.context_binding,
                )
            accepted_sequence = record.accepted_sequence
            if accepted_sequence is None and record.result is not None:
                accepted_sequence = getattr(record.result, "accepted_sequence", None)
            return _inspection(
                operation_id=operation_id,
                operation_type=record.operation_type,
                outcome=record.outcome,
                execution_id=session.execution_id,
                session_id=session.handle.execution_session_id,
                accepted_sequence=accepted_sequence,
                context_binding=session.handle.context_binding,
            )

    def enable_handoff_writer(
        self,
        handle: AdapterSessionHandle,
        *,
        fencing_token: int,
        operation_id: UUID,
    ) -> DeliveryResult:
        if not isinstance(fencing_token, int) or isinstance(fencing_token, bool):
            raise TypeError("fencing_token must be an integer")
        if fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        _require_handoff_profile(handle.capability_snapshot)
        session = self._session(handle)
        with session.lock:
            self._assert_session_identity(session, handle)
            previous = session.handoff_operations.get(operation_id)
            if previous is not None:
                if previous.operation_type is not GatewayOperationType.HANDOFF_ENABLE_WRITER:
                    raise OpenCodeAdapterError("handoff_operation_id_reused")
                if isinstance(previous.result, DeliveryResult):
                    return previous.result
                raise OpenCodeAdapterError("handoff_writer_result_unknown")
            if session.context_binding is None or not session.handoff_held:
                raise OpenCodeAdapterError("handoff_context_binding_missing")
            if fencing_token != session.fencing_token:
                raise OpenCodeAdapterError("handoff_fencing_mismatch")
            if session.stopped:
                raise OpenCodeAdapterError("session_stopped")
            if session.handoff_state_unknown:
                raise OpenCodeAdapterError("handoff_state_unknown")
            if session.writer_enabled:
                raise OpenCodeAdapterError("handoff_writer_already_enabled")
            if session.handoff_instruction is None:
                raise OpenCodeAdapterError("handoff_context_missing_instruction")
            instruction = session.handoff_instruction
            self._remember_handoff_operation(
                session,
                operation_id,
                GatewayOperationType.HANDOFF_ENABLE_WRITER,
                None,
                outcome=HandoffInspectionOutcome.IN_PROGRESS,
            )
            try:
                self._set_mode(
                    session.transport,
                    session.handle.adapter_session_id,
                    "build",
                )
                result = session.transport.request(
                    "session/prompt",
                    {
                        "sessionId": session.handle.adapter_session_id,
                        "prompt": _prompt_content(
                            _resume_control_prompt(
                                operation_id=operation_id,
                                binding=session.context_binding,
                                instruction=instruction,
                            ),
                            (),
                        ),
                    },
                    timeout=self._handoff_request_timeout_seconds,
                )
                _require_prompt_completion(result)
                if session.transport.process.poll() is not None:
                    raise OpenCodeAdapterError("handoff_writer_process_exited")
                self._append_notifications(session)
                session.writer_enabled = True
                sequence = session.next_sequence
                session.next_sequence += 1
                session.last_accepted_sequence = sequence
                session.events.append(
                    self._event(
                        session,
                        "handoff.writer_enabled",
                        sequence,
                        {"context_version": session.context_binding.context_version},
                    )
                )
                session.handle = _with_last_sequence(session.handle, sequence)
                delivery = DeliveryResult(
                    operation_id=operation_id,
                    outcome=DeliveryOutcome.DELIVERED,
                    accepted_sequence=sequence,
                    failure_code=None,
                    execution_session_id=session.handle.execution_session_id,
                )
                self._set_handoff_operation_result(
                    session,
                    operation_id,
                    delivery,
                    outcome=HandoffInspectionOutcome.SUCCEEDED,
                )
                return delivery
            except Exception:
                session.handoff_state_unknown = True
                self._set_handoff_operation_outcome(
                    session, operation_id, HandoffInspectionOutcome.UNKNOWN
                )
                raise

    def active_processes(self) -> tuple[Popen[Any], ...]:
        with self._lock:
            return tuple(
                session.transport.process
                for session in self._sessions.values()
                if session.transport.process.poll() is None
            )

    def _spawn(self, executable: str, cwd: Path, *, probe: bool) -> ACPProcessTransport:
        try:
            opencode_config: dict[str, object] = {
                "disabled_providers": ["deepseek"],
                "enabled_providers": ["opencode-go"],
                "permission": {"external_directory": "allow"},
            }
            if self._model is not None:
                opencode_config["model"] = self._model
            env = {
                "OPENCODE_CONFIG_CONTENT": json.dumps(
                    opencode_config, separators=(",", ":")
                )
            }
            return ACPProcessTransport.spawn(
                executable,
                cwd=cwd,
                stderr_limit_bytes=self._stderr_limit_bytes,
                env=env,
            )
        except OpenCodeAdapterError as exc:
            if probe:
                raise AdapterProbeError(str(exc)) from exc
            raise

    def _workspace_path(self, manifest: ExecutionWorkspaceManifest) -> Path:
        if self._workspace_root is not None:
            path = (self._workspace_root / str(manifest.execution_id)).resolve()
            if self._workspace_root not in path.parents:
                raise OpenCodeAdapterError("workspace location escapes configured root")
        elif manifest.location_ref.startswith("/"):
            path = Path(manifest.location_ref).resolve()
        else:
            raise OpenCodeAdapterError(
                "opaque workspace reference requires an adapter workspace root"
            )
        if not path.is_dir() or path.is_symlink():
            raise OpenCodeAdapterError(f"OpenCode workspace is unavailable: {path}")
        return path

    def _initialize_params(self, config: Mapping[str, object]) -> dict[str, object]:
        return {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {
                "name": self._client_name,
                "version": self._client_version,
            },
            "_meta": {"sidera": {"config": _redacted_config(config)}},
        }

    @staticmethod
    def _validate_initialize(result: Mapping[str, Any]) -> None:
        if result.get("protocolVersion") != 1:
            raise ACPProtocolError("ACP protocol version 1 was not negotiated")
        if not isinstance(result.get("agentCapabilities", {}), Mapping):
            raise ACPProtocolError("ACP initialize response has invalid capabilities")

    def _try_negotiate_handoff_modes(
        self,
        transport: ACPProcessTransport,
        session_id: str,
        session_result: Mapping[str, Any],
        *,
        keep_plan: bool,
    ) -> bool:
        modes = _session_modes(session_result)
        if not {"plan", "build"}.issubset(modes):
            return False
        try:
            self._set_mode(transport, session_id, "plan")
            if not keep_plan:
                self._set_mode(transport, session_id, "build")
        except (ACPProtocolError, ACPTimeoutError, OpenCodeAdapterError):
            return False
        return True

    def _set_mode(
        self, transport: ACPProcessTransport, session_id: str, mode_id: str
    ) -> None:
        transport.request(
            "session/set_mode",
            {"sessionId": session_id, "modeId": mode_id},
            timeout=self._handoff_request_timeout_seconds,
        )

    def _session(self, handle: AdapterSessionHandle) -> "_OpenCodeSession":
        with self._lock:
            try:
                return self._sessions[handle.adapter_session_id]
            except KeyError as exc:
                raise OpenCodeAdapterError("unknown OpenCode session") from exc

    @staticmethod
    def _assert_session_identity(
        session: "_OpenCodeSession", handle: AdapterSessionHandle
    ) -> None:
        if session.handle.execution_session_id != handle.execution_session_id:
            raise OpenCodeAdapterError("session_identity_mismatch")

    @staticmethod
    def _remember_handoff_operation(
        session: "_OpenCodeSession",
        operation_id: UUID,
        operation_type: GatewayOperationType,
        result: HandoffCheckpointEvidence | DeliveryResult | None,
        *,
        outcome: HandoffInspectionOutcome = HandoffInspectionOutcome.SUCCEEDED,
    ) -> None:
        session.handoff_operations[operation_id] = _OpenCodeHandoffOperation(
            operation_type=operation_type,
            outcome=outcome,
            result=result,
            accepted_sequence=(
                getattr(result, "accepted_sequence", None) if result is not None else None
            ),
        )

    @staticmethod
    def _set_handoff_operation_result(
        session: "_OpenCodeSession",
        operation_id: UUID,
        result: HandoffCheckpointEvidence | DeliveryResult,
        *,
        outcome: HandoffInspectionOutcome,
    ) -> None:
        operation = session.handoff_operations[operation_id]
        operation.result = result
        operation.accepted_sequence = getattr(result, "accepted_sequence", None)
        operation.outcome = outcome

    @staticmethod
    def _set_handoff_operation_outcome(
        session: "_OpenCodeSession",
        operation_id: UUID,
        outcome: HandoffInspectionOutcome,
    ) -> None:
        operation = session.handoff_operations[operation_id]
        operation.outcome = outcome

    def _append_notifications(self, session: "_OpenCodeSession") -> None:
        for message in session.transport.drain_notifications():
            event = _notification_event(
                message,
                session_id=session.handle.adapter_session_id,
            )
            if event is None:
                continue
            session.events.append(
                self._event(
                    session,
                    event.event_type,
                    session.next_sequence,
                    event.payload,
                    occurred_at=event.occurred_at,
                )
            )
            session.next_sequence += 1

    def _observe_terminal_locked(
        self, session: "_OpenCodeSession"
    ) -> AdapterEvent | None:
        if session.stopped or session.transport.process.poll() is None:
            return None
        session.stopped = True
        sequence = session.next_sequence
        session.next_sequence += 1
        return self._event(
            session,
            "session.failed",
            sequence,
            {"failure_code": "process_exited"},
        )

    @staticmethod
    def _event(
        session: "_OpenCodeSession",
        event_type: str,
        sequence: int,
        payload: Mapping[str, object],
        *,
        occurred_at: datetime | None = None,
    ) -> AdapterEvent:
        return AdapterEvent(
            event_type=event_type,
            sequence=sequence,
            payload=payload,
            occurred_at=occurred_at or datetime.now(UTC),
        )


class _OpenCodeSession:
    def __init__(
        self,
        *,
        handle: AdapterSessionHandle,
        transport: ACPProcessTransport,
        identity: ProcessDescriptor,
        cwd: Path,
        acceptance_criteria: tuple[str, ...],
        execution_id: UUID,
        fencing_token: int,
        workspace_digest: str,
        context_package: ContextMaterialization | None,
        context_binding: SessionContextBinding | None,
        handoff_instruction: str | None,
        handoff_held: bool,
        handoff_supported: bool,
    ) -> None:
        self.handle = handle
        self.transport = transport
        self.identity = identity
        self.cwd = cwd
        self.acceptance_criteria = acceptance_criteria
        self.execution_id = execution_id
        self.fencing_token = fencing_token
        self.workspace_digest = workspace_digest
        self.context_package = context_package
        self.context_binding = context_binding
        self.handoff_instruction = handoff_instruction
        self.handoff_held = handoff_held
        self.handoff_supported = handoff_supported
        self.events: deque[AdapterEvent] = deque()
        self.next_sequence = 1
        self.last_accepted_sequence = 0
        self.prompt_busy = False
        self.stopped = False
        self.checkpointed = False
        self.checkpoint_sequence = 0
        self.expected_stop_operation_id: UUID | None = None
        self.writer_enabled = False
        self.handoff_state_unknown = False
        self.handoff_operations: dict[UUID, _OpenCodeHandoffOperation] = {}
        self.lock = threading.RLock()


@dataclass(slots=True)
class _OpenCodeHandoffOperation:
    operation_type: GatewayOperationType
    outcome: HandoffInspectionOutcome
    result: HandoffCheckpointEvidence | DeliveryResult | None
    accepted_sequence: int | None


def _validate_context(
    materialization: ContextMaterialization,
    operation_id: UUID,
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
    if materialization.schema_version not in (1, 2, 3):
        raise ValueError("unsupported context materialization schema")
    if (
        materialization.metadata.purpose is not ContextPurpose.EXECUTION
        or materialization.metadata.consumer_type is not ContextConsumerType.WORKER
    ):
        raise ValueError("worker adapter requires an execution worker context")
    if materialization.total_byte_count > 256 * 1024:
        raise ValueError("context materialization exceeds adapter size limit")


def _declared_context_package_versions(
    versions: tuple[int, ...],
) -> tuple[int, ...]:
    if not isinstance(versions, tuple):
        raise TypeError("context_package_versions must be a tuple")
    normalized = tuple(sorted(set(versions)))
    if not normalized:
        raise ValueError("context_package_versions cannot be empty")
    if any(
        not isinstance(version, int) or isinstance(version, bool) or version < 1
        for version in normalized
    ):
        raise ValueError("context_package_versions must contain positive integers")
    unsupported = set(normalized).difference({1, 2, 3})
    if unsupported:
        raise ValueError(
            "unsupported OpenCode context package versions: "
            + ", ".join(str(version) for version in sorted(unsupported))
        )
    return normalized


def _require_handoff_profile(profile: WorkerAdapterCapabilityProfile) -> None:
    if not handoff_profile_supported(profile):
        raise OpenCodeHandoffUnsupported("opencode_handoff_unsupported")


def _handoff_facts(
    materialization: ContextMaterialization | None,
) -> Mapping[str, object] | None:
    if materialization is None:
        return None
    handoff = materialization.control_plane_facts.facts.get("handoff")
    return handoff if isinstance(handoff, Mapping) else None


def _handoff_instruction(facts: Mapping[str, object]) -> str:
    instruction = facts.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise OpenCodeAdapterError("handoff_context_missing_instruction")
    value = instruction.strip()
    if len(value) > 4096:
        raise OpenCodeAdapterError("handoff_instruction_too_large")
    return value


def _context_refs(
    materialization: ContextMaterialization, *, category: str
) -> tuple[str, ...]:
    return tuple(
        f"{category}://{ref.ref_id}"
        for ref in materialization.artifact_and_evidence_refs
        if ref.category == category
    )


def _session_modes(result: Mapping[str, Any]) -> frozenset[str]:
    options = result.get("configOptions")
    if not isinstance(options, (list, tuple)):
        return frozenset()
    for option in options:
        if not isinstance(option, Mapping) or option.get("id") != "mode":
            continue
        values = option.get("options")
        if not isinstance(values, (list, tuple)):
            return frozenset()
        modes = {
            str(item.get("value"))
            for item in values
            if isinstance(item, Mapping) and isinstance(item.get("value"), str)
        }
        return frozenset(modes)
    return frozenset()


def _require_prompt_completion(result: Mapping[str, Any]) -> None:
    if result.get("stopReason") != "end_turn":
        raise ACPProtocolError("OpenCode prompt completion was not proven")


def _checkpoint_control_prompt(
    *,
    operation_id: UUID,
    transition_id: UUID,
    source_input_cutoff_sequence: int,
) -> str:
    return (
        "SIDERA_HANDOFF_CHECKPOINT_V1\n"
        "This is a system-owned control event. Do not perform new work, modify files, "
        "or accept user input. Confirm the current execution is quiescent.\n"
        f"operation_id={operation_id}\n"
        f"transition_id={transition_id}\n"
        f"source_input_cutoff_sequence={source_input_cutoff_sequence}"
    )


def _resume_control_prompt(
    *,
    operation_id: UUID,
    binding: SessionContextBinding,
    instruction: str,
) -> str:
    return (
        "SIDERA_HANDOFF_RESUME_V1\n"
        "This is a system-owned control event. Continue only from the bound context "
        "package after the writer gate is enabled.\n"
        f"operation_id={operation_id}\n"
        f"context_binding={binding.package_id}:{binding.package_hash}:"
        f"{binding.context_version}\n"
        f"instruction={instruction}"
    )


def _inspection(
    *,
    operation_id: UUID,
    operation_type: GatewayOperationType,
    outcome: HandoffInspectionOutcome,
    execution_id: UUID | None,
    session_id: UUID | None,
    accepted_sequence: int | None,
    context_binding: SessionContextBinding | None,
) -> HandoffOperationInspection:
    evidence_refs = (
        f"opencode-inspection://{operation_id}/{outcome.value}",
    )
    return HandoffOperationInspection(
        operation_id=operation_id,
        operation_type=operation_type,
        outcome=outcome,
        execution_id=execution_id,
        session_id=session_id,
        accepted_sequence=accepted_sequence,
        context_binding=context_binding,
        observed_at=datetime.now(UTC),
        evidence_refs=evidence_refs,
    )


def _config_text(config: Mapping[str, object], key: str, default: str) -> str:
    for candidate in (key, f"opencode.{key}"):
        value = config.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _config_path(config: Mapping[str, object], key: str, default: Path) -> Path:
    value = _config_text(config, key, str(default))
    return Path(value).expanduser().resolve()


def _config_mcp_servers(config: Mapping[str, object]) -> list[object]:
    value = config.get("mcp_servers", config.get("opencode.mcp_servers", []))
    if isinstance(value, list):
        return value
    return []


def _redacted_config(config: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): "[redacted]" if _SECRET_KEY.search(str(key)) else value
        for key, value in config.items()
    }


def _prompt_content(
    text: str, attachment_refs: tuple[str, ...]
) -> list[dict[str, object]]:
    content: list[dict[str, object]] = [{"type": "text", "text": text}]
    content.extend({"type": "resource_link", "uri": ref} for ref in attachment_refs)
    return content


def _session(result: Mapping[str, Any]) -> str:
    value = result.get("sessionId")
    if not isinstance(value, str) or not value.strip():
        raise ACPProtocolError("ACP session/new response has no sessionId")
    return value


def _with_last_sequence(
    handle: AdapterSessionHandle, sequence: int
) -> AdapterSessionHandle:
    return AdapterSessionHandle(
        execution_session_id=handle.execution_session_id,
        adapter_session_id=handle.adapter_session_id,
        process_ref=handle.process_ref,
        capability_snapshot=handle.capability_snapshot,
        last_accepted_sequence=sequence,
        started_at=handle.started_at,
        context_binding=handle.context_binding,
    )


def _notification_event(
    message: Mapping[str, Any], *, session_id: str
) -> AdapterEvent | None:
    if message.get("method") != "session/update":
        return None
    params = message.get("params")
    if not isinstance(params, Mapping) or params.get("sessionId") != session_id:
        return None
    update = params.get("update")
    if not isinstance(update, Mapping):
        return None
    update_type = str(update.get("sessionUpdate", "unknown"))
    event_type = {
        "usage_update": "session.heartbeat",
        "plan": "progress.updated",
        "agent_message_chunk": "progress.updated",
        "tool_call": "progress.updated",
        "tool_call_update": "progress.updated",
    }.get(update_type, "progress.updated")
    payload = dict(update)
    payload["trace_category"] = _trace_category(update_type, update)
    summary = _trace_summary(update)
    if summary:
        payload["trace_summary"] = summary
    usage = update.get("usage")
    if isinstance(usage, Mapping):
        payload["trace_usage"] = dict(usage)
    return AdapterEvent(
        event_type=event_type,
        sequence=1,
        payload=payload,
        occurred_at=datetime.now(UTC),
    )


def _trace_category(update_type: str, update: Mapping[str, Any]) -> str:
    if update_type in {"agent_message_chunk", "plan"}:
        return "assistant_summary"
    if update_type == "usage_update":
        return "model"
    if update_type == "tool_call_update":
        return "subtool" if update.get("parent_tool") else "tool"
    if update_type == "tool_call":
        return "tool"
    return "model"


def _trace_summary(update: Mapping[str, Any]) -> str | None:
    for key in ("summary", "text", "title", "tool", "name"):
        value = update.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:4096]
    content = update.get("content")
    if isinstance(content, Mapping):
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()[:4096]
    return None


def _process_identity(process: Popen[Any]) -> ProcessDescriptor:
    try:
        process_group_id = os.getpgid(process.pid)
    except OSError as exc:
        raise OpenCodeAdapterError(
            "OpenCode process group identity is unavailable"
        ) from exc
    return ProcessDescriptor(
        pid=process.pid,
        started_at=datetime.now(UTC),
        nonce=uuid4().hex,
        process_group_id=process_group_id,
    )


def _identity_matches(identity: ProcessDescriptor) -> bool:
    try:
        os.kill(identity.pid, 0)
        if os.getpgid(identity.pid) != identity.process_group_id:
            return False
    except OSError:
        return False
    observed = _observed_process_start(identity.pid)
    if observed is None:
        return False
    return abs((observed - identity.started_at).total_seconds()) <= 3


def _observed_process_start(pid: int) -> datetime | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b  %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(value, fmt).replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
        except ValueError:
            continue
    return None


def _terminate_verified_process(
    process: Popen[Any],
    identity: ProcessDescriptor,
    *,
    grace_seconds: float,
) -> bool:
    if process.poll() is not None:
        return True
    if not _identity_matches(identity):
        return False
    try:
        os.killpg(identity.process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return process.poll() is not None
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is not None:
        return True
    if not _identity_matches(identity):
        return False
    try:
        os.killpg(identity.process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return process.poll() is not None
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return False
    return process.poll() is not None


def _terminate_owned_process(process: Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _rpc_error(error: object) -> str:
    if isinstance(error, Mapping):
        code = error.get("code", "unknown")
        message = error.get("message", "ACP request failed")
        return f"{code}: {message}"
    return "ACP request failed"


def _failure_code(error: Exception) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(error).lower()).strip("_")
    return value[:100] or "acp_error"


def _git_workspace_diff(git_executable: str, cwd: Path) -> bytes:
    tracked = subprocess.run(
        [
            git_executable,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-color",
            "HEAD",
            "--",
            ".",
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    untracked = subprocess.run(
        [
            git_executable,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    chunks = [tracked.stdout]
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative_path = Path(os.fsdecode(raw_path))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise OpenCodeAdapterError("Git returned an unsafe untracked path")
        command = [
            git_executable,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-color",
            "--no-index",
            "--",
            os.devnull,
            str(relative_path),
        ]
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if result.returncode not in (0, 1):
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
        chunks.append(result.stdout)
    return b"".join(chunks)


_SECRET_KEY = re.compile(r"(key|token|secret|password|credential)", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)\s*([=:])\s*([^\s,;]+)"
)


def _redact_stderr(value: str) -> str:
    return _SECRET_VALUE.sub(r"\1\2[redacted]", value)
