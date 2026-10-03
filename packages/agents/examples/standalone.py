"""Provider-free public consumer with a trusted test-only native boundary."""

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from sidera_agents.api import AgentCatalog, AgentExecution, AgentObservations
from sidera_agents.bootstrap import (
    create_agent_service,
    create_harness_registry,
    open_memory_store,
)
from sidera_agents.contracts import (
    AssembledInput,
    CapabilityResourceDraft,
    CapabilitySupport,
    ConfigValidation,
    ExecutionWorkspace,
    NativeCancelOutcome,
    NativeCancelState,
    NativeCloseOutcome,
    NativeCloseState,
    NativeEvent,
    NativeHandle,
    NativeInspection,
    NativeOpenRequest,
    NativeOutcome,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    ProcessIdentity,
    ProfileDraft,
    ProfileRevision,
    ProtocolState,
    ResourceKind,
    RunPolicy,
    RunStart,
    RunState,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeProbe,
    RuntimeSnapshot,
    StartStatus,
    SupportStatus,
)
from sidera_agents.errors import AgentError
from sidera_agents.ports import (
    DenyContextSourceReader,
    DenySecretReader,
    HarnessAdapter,
)


class FixtureHarness:
    """Simulates native evidence; does not spawn a CLI or contact a model."""

    harness_id = "fixture-harness"
    executable_name = "fixture-harness"

    def __init__(self) -> None:
        self.open_count = 0
        self.prompt_count = 0
        self.close_count = 0
        self._events: dict[UUID, tuple[NativeEvent, ...]] = {}
        self._closed: set[UUID] = set()

    def describe(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            harness_id=self.harness_id,
            display_name="Test-only fixture harness",
            fields=(),
            model_options=(),
            thinking_options=(),
        )

    def probe(self, candidate: RuntimeCandidate, checked_at: datetime) -> RuntimeProbe:
        return RuntimeProbe(
            actual_version="1.0",
            protocol_state=ProtocolState.READY,
            capabilities={
                "prompt": CapabilitySupport(
                    status=SupportStatus.SUPPORTED,
                    reason_code="fixture_only",
                    constraints={},
                )
            },
            checked_at=checked_at,
        )

    def validate(
        self, profile: ProfileRevision, runtime: RuntimeSnapshot
    ) -> ConfigValidation:
        return ConfigValidation(valid=True, issues=(), effective_preview=None)

    def open(self, request: NativeOpenRequest) -> NativeHandle:
        self.open_count += 1
        self._events[request.run_id] = ()
        return NativeHandle(
            run_id=request.run_id,
            session_id="fixture-session",
            process_identity=ProcessIdentity(
                pid=1,
                start_identity="simulated-fixture-process",
                owned_nonce=uuid4(),
            ),
            capabilities=("prompt",),
        )

    def prompt(
        self, handle: NativeHandle, input: AssembledInput, response_id: UUID
    ) -> NativeOutcome:
        self.prompt_count += 1
        self._events[handle.run_id] = (
            NativeEvent(
                cursor=1,
                native_id="fixture-message",
                kind="message",
                response_id=response_id,
                operation_id=None,
                occurred_at=datetime.now(UTC),
                data={"text": "Fixture response."},
            ),
        )
        return NativeOutcome(
            response_id=response_id,
            status=NativeOutcomeStatus.COMPLETED,
            reason_code=None,
            observed_model=None,
            usage=None,
        )

    def read_events(
        self, handle: NativeHandle, after_cursor: int, limit: int = 100
    ) -> tuple[NativeEvent, ...]:
        return tuple(
            event
            for event in self._events[handle.run_id]
            if event.cursor > after_cursor
        )[:limit]

    def cancel(self, handle: NativeHandle, response_id: UUID) -> NativeCancelOutcome:
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.UNSUPPORTED,
            reason_code="fixture_cancel_unsupported",
        )

    def inspect(self, handle: NativeHandle) -> NativeInspection:
        return NativeInspection(
            process_state=(
                NativeProcessState.GONE
                if handle.run_id in self._closed
                else NativeProcessState.ALIVE
            ),
            response_state=NativeResponseState.IDLE,
            reason_code=None,
        )

    def close(self, handle: NativeHandle) -> NativeCloseOutcome:
        self.close_count += 1
        self._closed.add(handle.run_id)
        return NativeCloseOutcome(status=NativeCloseState.CLOSED, reason_code=None)


class WorkspaceAuthorizer:
    def __init__(self, workspace_id: UUID, grant_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.grant_id = grant_id

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        allowed = {
            "profile.create",
            "profile.read",
            "profile.revise",
            "profile.disable",
            "resource.create",
            "resource.read",
            "resource.revise",
            "resource.disable",
            "runtime.register",
            "runtime.read",
            "run.start",
            "run.read",
        }
        if (
            grant_id != self.grant_id
            or action not in allowed
            or getattr(target, "workspace_id", target) != self.workspace_id
        ):
            raise AgentError("permission_denied", "Access denied.")


def main() -> dict[str, object]:
    workspace_id, grant_id = uuid4(), uuid4()
    fixture = FixtureHarness()
    adapter: HarnessAdapter = fixture
    service = create_agent_service(
        open_memory_store(),
        create_harness_registry((adapter,)),
        WorkspaceAuthorizer(workspace_id, grant_id),
        DenyContextSourceReader(),
        DenySecretReader(),
        lambda: datetime.now(UTC),
    )
    catalog: AgentCatalog = service.catalog
    execution: AgentExecution = service.execution
    observations: AgentObservations = service.observations
    with TemporaryDirectory(prefix="sidera-agents-example-") as temporary:
        root = Path(temporary)
        executable = root / "fixture-harness"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        executable.chmod(0o700)
        workspace = root / "workspace"
        workspace.mkdir()
        try:
            assert service.readiness() is True
            assert service.readiness(require_execution_owner=True) is False
            runtime = service.runtimes.register(
                workspace_id,
                adapter.harness_id,
                str(executable),
                max_active=1,
                max_sessions=1,
                grant_id=grant_id,
            )
            resource = catalog.create_resource(
                workspace_id,
                CapabilityResourceDraft(
                    name="fixture note",
                    kind=ResourceKind.SKILL,
                    settings={"body": "Local fixture note."},
                    secret_refs=(),
                ),
                grant_id=grant_id,
            )
            profile = catalog.create_profile(
                workspace_id,
                ProfileDraft(
                    name="fixture profile",
                    description="Provider-free standalone example",
                    runtime_ref=runtime.ref,
                    instructions="Respond to the fixture request.",
                    resource_refs=(),
                    model_id=None,
                    thinking=None,
                    max_active=1,
                    environment={},
                    secret_refs=(),
                    native_settings={},
                ),
                grant_id=grant_id,
            )
            assert (
                catalog.get_profile_status(profile.ref, grant_id=grant_id).version == 1
            )
            assert (
                catalog.get_resource_status(resource.ref, grant_id=grant_id).version
                == 1
            )
            revised_profile = catalog.revise_profile(
                profile.ref, profile.draft, grant_id=grant_id
            )
            revised_resource = catalog.revise_resource(
                resource.ref, resource.draft, grant_id=grant_id
            )
            service.poll_once()
            assert service.readiness(require_execution_owner=True) is True
            receipt = execution.start(
                RunStart(
                    operation_id=uuid4(),
                    profile_ref=profile.ref,
                    grant_id=grant_id,
                    policy=RunPolicy.BOUNDED,
                    instructions=(),
                    text="Fixture request.",
                    context=(),
                    tools=(),
                    workspace=ExecutionWorkspace(
                        directory=str(workspace),
                        writable=True,
                        artifact_directory=None,
                    ),
                    required_capabilities=("prompt",),
                )
            )
            assert receipt.status is StartStatus.ACCEPTED and receipt.run_id is not None
            service.poll_once()
            snapshot = execution.get_run(
                workspace_id, receipt.run_id, grant_id=grant_id
            )
            assert snapshot.state is RunState.COMPLETED
            page = observations.read_trace(
                workspace_id, receipt.run_id, 0, grant_id=grant_id
            )
            assert page == execution.list_events(
                workspace_id, receipt.run_id, 0, grant_id=grant_id
            )
            assert (fixture.open_count, fixture.prompt_count, fixture.close_count) == (
                1,
                1,
                1,
            )
            catalog.set_profile_enabled(
                workspace_id, profile.ref.profile_id, False, 1, grant_id=grant_id
            )
            catalog.set_resource_enabled(
                workspace_id, resource.ref.resource_id, False, 1, grant_id=grant_id
            )
            profile_status = catalog.get_profile_status(profile.ref, grant_id=grant_id)
            resource_status = catalog.get_resource_status(
                resource.ref, grant_id=grant_id
            )
            assert profile_status == catalog.get_profile_status(
                revised_profile.ref, grant_id=grant_id
            )
            assert resource_status == catalog.get_resource_status(
                revised_resource.ref, grant_id=grant_id
            )
            assert catalog.get_profile(profile.ref, grant_id=grant_id) == profile
            assert catalog.get_resource(resource.ref, grant_id=grant_id) == resource
            service.close()
            assert (
                observations.read_trace(
                    workspace_id, receipt.run_id, 0, grant_id=grant_id
                )
                == page
            )
            return {
                "harness_id": adapter.harness_id,
                "state": snapshot.state.value,
                "messages": [
                    event.data["text"]
                    for event in page.events
                    if event.kind == "message"
                ],
                "closed": fixture.close_count == 1,
                "profile_status": {
                    "enabled": profile_status.enabled,
                    "version": profile_status.version,
                },
                "resource_status": {
                    "enabled": resource_status.enabled,
                    "version": resource_status.version,
                },
            }
        finally:
            service.close()


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True))
