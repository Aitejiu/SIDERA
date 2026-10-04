"""Translate public native Runs at the existing CP worker boundary."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    model_validator,
)
from sidera_agents.api import (
    AgentCatalog,
    AgentExecution,
    AgentObservations,
    AgentRuntimes,
)
from sidera_agents.contracts import (
    CredentialRef,
    InputStatus,
    ProfileRef,
    ResourceKind,
    ResourceRef,
    RunEvent,
    RunStart,
    RunStop,
    RuntimeRef,
    SourceRef,
    StartReceipt,
    ToolBinding,
)
from sidera_agents.errors import AgentError
from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_ports import CodeInputReceipt
from sidera_control_plane.domain.agent_bindings import AgentExecutionLink
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.gateway import (
    AdapterEvent,
    AdapterSessionHandle,
    DeliveryOutcome,
    DeliveryResult,
)
from sidera_control_plane.domain.inputs import input_operation_id
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.result_operations import ResultError


class NativeSourceValidation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ref: SourceRef
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validated_bytes: int = Field(ge=0)


class NativeContextPreparationReceipt(BaseModel):
    """CP source and physical preparation, never final prompt/model-read proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    command_id: UUID
    workspace_id: UUID
    execution_id: UUID
    binding_version: int = Field(ge=1)
    authority_id: UUID
    lease_id: UUID
    fencing_token: int = Field(ge=1)
    package_id: UUID
    package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_id: UUID
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_version: int = Field(ge=1)
    sources: tuple[NativeSourceValidation, ...]
    validated_bytes: int = Field(ge=0)
    validated_sections: int = Field(ge=0)
    workspace_manifest_id: UUID
    workspace_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_location_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_input: CodeInputReceipt | None
    capture_operation_id: UUID
    phases: tuple[str, ...] = ("source_validation", "physical_preparation")

    @field_serializer("physical_input")
    def serialize_physical_input(self, receipt):
        return None if receipt is None else receipt.to_mapping()

    @model_validator(mode="after")
    def validate_preparation(self):
        if (
            self.phases != ("source_validation", "physical_preparation")
            or self.validated_sections != len(self.sources)
            or self.validated_bytes
            != sum(item.validated_bytes for item in self.sources)
            or len({item.ref for item in self.sources}) != len(self.sources)
            or (
                self.physical_input is not None
                and (
                    self.physical_input.execution_id != self.execution_id
                    or not self.physical_input.succeeded
                )
            )
        ):
            raise ValueError("Native preparation evidence is inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class NativeDeferredResult:
    workspace_id: UUID
    execution_id: UUID
    status: str
    start_operation_id: UUID


@dataclass(frozen=True, slots=True)
class NativeWorkerServices:
    catalog: AgentCatalog
    runtimes: AgentRuntimes
    execution: AgentExecution
    authorizer: ExecutionAuthorizer
    principal_id: str
    catalog_grant_id: UUID
    observations: AgentObservations | None = None


@dataclass(frozen=True, slots=True)
class NativeAuthorityPins:
    runtime_ref: RuntimeRef
    credentials: tuple[CredentialRef, ...]
    resources: tuple[ResourceRef, ...]
    tools: tuple[ToolBinding, ...]
    actions: tuple[str, ...]


def native_authority_pins(
    services: NativeWorkerServices, ref: ProfileRef
) -> NativeAuthorityPins:
    profile = services.catalog.get_profile(ref, grant_id=services.catalog_grant_id)
    status = services.catalog.get_profile_status(
        ref, grant_id=services.catalog_grant_id
    )
    if (
        profile.ref != ref
        or status.profile_id != ref.profile_id
        or not status.enabled
        or profile.draft.runtime_ref.workspace_id != ref.workspace_id
    ):
        raise AgentError("configuration_mismatch", "Pinned Profile is unavailable.")
    credentials = [binding.credential_ref for binding in profile.draft.secret_refs]
    resources, tools = profile.draft.resource_refs, []
    for resource_ref in resources:
        if resource_ref.workspace_id != ref.workspace_id:
            raise AgentError(
                "permission_denied", "Pinned resource is outside its workspace."
            )
        resource = services.catalog.get_resource(
            resource_ref, grant_id=services.catalog_grant_id
        )
        state = services.catalog.get_resource_status(
            resource_ref, grant_id=services.catalog_grant_id
        )
        if (
            resource.ref != resource_ref
            or state.resource_id != resource_ref.resource_id
            or not state.enabled
        ):
            raise AgentError(
                "configuration_mismatch", "Pinned resource is unavailable."
            )
        credentials.extend(
            binding.credential_ref for binding in resource.draft.secret_refs
        )
        if resource.draft.kind is ResourceKind.MCP:
            tools.append(ToolBinding(resource_ref=resource_ref))
    credentials = tuple(dict.fromkeys(credentials))
    if any(credential.workspace_id != ref.workspace_id for credential in credentials):
        raise AgentError(
            "permission_denied", "Pinned credential is outside its workspace."
        )
    actions = tuple(
        action
        for action, pins in (
            ("secret.read", credentials),
            ("resource.read", resources),
            ("tool.execute", tools),
        )
        if pins
    )
    return NativeAuthorityPins(
        profile.draft.runtime_ref, credentials, resources, tuple(tools), actions
    )


class NativeStartDeferred(RuntimeError):
    def __init__(self, link):
        self.link = link
        self.status = link.status
        super().__init__("Native admission is not yet confirmed.")


def native_operation_id(execution_id, binding_version, kind="start"):
    return uuid5(
        NAMESPACE_URL, f"sidera.native.{kind}:{execution_id}:{binding_version}"
    )


def native_preparation_command(
    receipt: NativeContextPreparationReceipt,
) -> CommandEnvelope:
    return CommandEnvelope(
        receipt.command_id,
        receipt.workspace_id,
        "PrepareNativeContextSources",
        "execution",
        "system",
        "gateway",
        receipt.execution_id,
        {"preparation": receipt.model_dump(mode="json")},
        None,
        f"native-context:{receipt.command_id}",
        receipt.command_id,
    )


def read_native_preparation_receipt(
    uow: ControlPlaneUnitOfWork,
    link: AgentExecutionLink,
) -> NativeContextPreparationReceipt:
    if link.preparation is None:
        raise ResultError("context_package_missing", {})
    try:
        receipt = NativeContextPreparationReceipt.model_validate(link.preparation)
    except (ValidationError, TypeError, ValueError):
        raise ResultError("context_hash_mismatch", {}) from None
    authority = uow.execution_authorities.get(
        link.authority_id, workspace_id=link.workspace_id
    )
    command = uow.commands.find_by_id(link.workspace_id, receipt.command_id)
    if authority is None or command is None:
        raise ResultError("context_package_missing", {})
    result_data = (
        command.result.get("data") if isinstance(command.result, Mapping) else None
    )
    if (
        command.status != "applied"
        or command.workspace_id != link.workspace_id
        or command.id != receipt.command_id
        or receipt.command_id
        != uuid5(
            NAMESPACE_URL,
            f"sidera.native.prepare-context:{link.execution_id}:{link.binding_version}:{receipt.package_id}:{receipt.package_hash}:{receipt.revision_hash}",
        )
        or receipt.capture_operation_id
        != native_operation_id(link.execution_id, link.binding_version, "code-capture")
        or command.request_hash != hash_request(native_preparation_command(receipt))
        or not isinstance(result_data, Mapping)
        or result_data.get("preparation") != receipt.model_dump(mode="json")
        or (
            authority.id,
            authority.workspace_id,
            authority.execution_id,
            authority.binding_version,
            authority.profile_ref,
        )
        != (
            link.authority_id,
            link.workspace_id,
            link.execution_id,
            link.binding_version,
            link.profile_ref,
        )
        or (
            receipt.workspace_id,
            receipt.execution_id,
            receipt.binding_version,
            receipt.authority_id,
            receipt.lease_id,
            receipt.fencing_token,
        )
        != (
            link.workspace_id,
            link.execution_id,
            link.binding_version,
            link.authority_id,
            authority.lease_id,
            authority.fencing_token,
        )
    ):
        raise ResultError("context_hash_mismatch", {})
    return receipt


def request_fingerprint(request: RunStart) -> str:
    body = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def native_binding(uow, execution):
    if execution.purpose == "verification":
        return None
    repository = getattr(uow, "agent_bindings", None)
    if repository is None:
        return None
    if execution.worker_item_id is not None:
        binding = repository.get(
            execution.workspace_id, "worker_item", execution.worker_item_id
        )
        if binding is not None:
            return binding
    return repository.get(execution.workspace_id, "subtask", execution.task_id)


def native_capabilities():
    # Business protocol support is not a Harness/model capability declaration.
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency="operation-id",
        resume="none",
        stop="exact-execution",
        sandbox="filesystem",
        stability="experimental",
        context_package_versions=(1, 2, 3),
        code_result_versions=(1,),
    )


def project_run_event(event: RunEvent, *, run_state=None):
    kind = f"native.{event.kind}"
    if event.kind in {"run.closed", "run.completed", "run.failed", "run.stopped"}:
        # A snapshot can precede this immutable event or change before replay.
        state = event.data.get("status", event.data.get("state"))
        declared = {
            "run.completed": "completed",
            "run.failed": "failed",
            "run.stopped": "stopped",
        }.get(event.kind)
        if state is None:
            state = declared
        if declared is not None and state != declared:
            state = None
        if not isinstance(state, str) or state not in {
            "completed",
            "failed",
            "stopped",
        }:
            state = None
        kind = (
            {
                "completed": "session.completed",
                "failed": "session.failed",
                "stopped": "session.closed",
            }.get(state, kind)
            if state is not None
            else kind
        )
    payload = dict(event.data)
    payload.update(run_id=str(event.run_id), native_kind=event.kind)
    if event.operation_id is not None:
        payload["operation_id"] = str(event.operation_id)
    if event.response_id is not None:
        payload["response_id"] = str(event.response_id)
    if kind == "session.completed":
        payload["outcome"] = "succeeded"
    canonical = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return AdapterEvent(
        kind,
        event.sequence,
        payload,
        event.occurred_at,
        raw_ref=f"native-event-sha256:{digest}",
    )


def require_gateway_owner(owner_check):
    if owner_check is not None and not owner_check():
        raise AgentError("gateway_owner_lost", "Native owner is no longer available.")


class AgentWorkerAdapter:
    def __init__(
        self,
        uow_factory,
        *,
        execution,
        input_builder,
        authorizer,
        workspace_id,
        execution_id,
        clock=None,
        capability_snapshot=None,
        owner_check=None,
    ):
        self._uow_factory, self._execution = uow_factory, execution
        self._builder, self._authorizer = input_builder, authorizer
        self._workspace_id, self._execution_id = workspace_id, execution_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._capabilities = capability_snapshot or native_capabilities()
        self._owner_check = owner_check

    def probe(self, config):
        return self._capabilities

    def _canonical(self, *, operation_id=None, run_id=None):
        with self._uow_factory() as uow:
            link = uow.execution_authorities.get_link(
                self._workspace_id, self._execution_id
            )
            authority = (
                None
                if link is None
                else uow.execution_authorities.get(
                    link.authority_id, workspace_id=self._workspace_id
                )
            )
            if (
                link is None
                or authority is None
                or authority.execution_id != self._execution_id
                or authority.profile_ref != link.profile_ref
                or authority.binding_version != link.binding_version
                or (
                    operation_id is not None and operation_id != link.start_operation_id
                )
                or (run_id is not None and run_id != link.run_id)
            ):
                raise AgentError(
                    "permission_denied", "Original native intention is unavailable."
                )
            return link

    def get_start(self, workspace_id, operation_id):
        if workspace_id != self._workspace_id:
            raise AgentError(
                "permission_denied", "Original native intention is unavailable."
            )
        link = self._canonical(operation_id=operation_id)
        self._authorizer.require(link.authority_id, "run.read", link.workspace_id)
        return self._execution.get_start(
            link.workspace_id, link.start_operation_id, grant_id=link.authority_id
        )

    def get_run(self, workspace_id, run_id):
        if workspace_id != self._workspace_id:
            raise AgentError("permission_denied", "Original native Run is unavailable.")
        link = self._canonical(run_id=run_id)
        self._authorizer.require(link.authority_id, "run.read", link.workspace_id)
        result = self._execution.get_run(
            link.workspace_id, link.run_id, grant_id=link.authority_id
        )
        if (
            result.run_id != link.run_id
            or result.workspace_id != link.workspace_id
            or result.profile_ref != link.profile_ref
        ):
            raise AgentError(
                "configuration_mismatch", "Original native Run is unavailable."
            )
        return result

    def _events(self, link, after_sequence):
        link = self._canonical(run_id=link.run_id)
        if link.run_id is None:
            raise AgentError("permission_denied", "Original native Run is unavailable.")
        self._authorizer.require(link.authority_id, "run.read", link.workspace_id)
        page = self._execution.list_events(
            link.workspace_id, link.run_id, after_sequence, grant_id=link.authority_id
        )
        if any(event.run_id != link.run_id for event in page.events):
            raise AgentError("configuration_mismatch", "Native event identity changed.")
        return page

    def start_session(self, spec, workspace, *, operation_id):
        require_gateway_owner(self._owner_check)
        if (
            spec.workspace_id != self._workspace_id
            or spec.execution_id != self._execution_id
        ):
            raise AgentError(
                "permission_denied", "Original native intention is unavailable."
            )
        link = self._canonical(operation_id=operation_id)
        if link.status in {"pending", "waiting_capacity"}:
            with self._uow_factory() as uow:
                binding = native_binding(
                    uow, uow.executions.get(self._execution_id, self._workspace_id)
                )
            if link.status == "pending":
                request = self._builder.build(
                    self._execution_id, binding, link.authority_id
                )
                sha = request_fingerprint(request)
            else:
                sha = link.request_sha256
            with self._uow_factory() as uow:
                link = uow.execution_authorities.begin_start(
                    link, request_sha256=sha, now=self._clock()
                )
            if link.start_receipt is None:
                request = self._builder.build(
                    self._execution_id, binding, link.authority_id
                )
                if (
                    request_fingerprint(request) != sha
                    or request.operation_id != operation_id
                ):
                    raise AgentError(
                        "native_request_changed",
                        "Native start request no longer matches its saved fingerprint.",
                    )
            require_gateway_owner(self._owner_check)
            try:
                receipt = self._execution.start(request)
            except Exception:  # noqa: BLE001 - an unconfirmed external admission is query-only
                receipt = StartReceipt(
                    operation_id=operation_id,
                    status="unknown",
                    run_id=None,
                    reason_code="native_start_unconfirmed",
                )
            require_gateway_owner(self._owner_check)
            try:
                with self._uow_factory() as uow:
                    link = uow.execution_authorities.record_start(
                        link, receipt, now=self._clock()
                    )
            except AgentError as exc:
                if exc.code == "gateway_owner_lost":
                    raise
                raise NativeStartDeferred(self._canonical()) from None
            except Exception:  # noqa: BLE001 - a lost persistence ACK cannot authorize replay
                # The saved starting intention remains query-only after a lost ACK.
                raise NativeStartDeferred(self._canonical()) from None
        elif link.run_id is None:
            try:
                lookup = self.get_start(link.workspace_id, link.start_operation_id)
            except Exception:  # noqa: BLE001 - failed lookup does not prove no admission
                raise NativeStartDeferred(link) from None
            if not lookup.found or lookup.receipt is None:
                raise NativeStartDeferred(link)
            require_gateway_owner(self._owner_check)
            with self._uow_factory() as uow:
                link = uow.execution_authorities.record_start(
                    link, lookup.receipt, now=self._clock()
                )
        if link.status == "failed":
            raise AgentError(
                "native_admission_rejected", "Native admission was rejected."
            )
        if link.run_id is None:
            raise NativeStartDeferred(link)
        page = self._events(link, 0)
        started = next(
            (event for event in page.events if event.kind == "run.started"), None
        )
        if started is None:
            raise NativeStartDeferred(link)
        return AdapterSessionHandle(
            execution_session_id=uuid5(
                NAMESPACE_URL, f"sidera.native.session:{link.run_id}"
            ),
            adapter_session_id=f"agent-run:{link.run_id}",
            process_ref=f"agent-run-reference:{link.run_id}",
            capability_snapshot=self._capabilities,
            last_accepted_sequence=0,
            started_at=started.occurred_at,
        )

    def read_events(self, handle):
        link = self._canonical()
        self._check_handle(link, handle)
        with self._uow_factory() as uow:
            session = uow.sessions.get(handle.execution_session_id, link.workspace_id)
            if session is None or session.execution_id != link.execution_id:
                raise AgentError(
                    "permission_denied", "Original native Session is unavailable."
                )
            sequence = min(session.last_sequence, link.event_cursor)
        snapshot = self.get_run(link.workspace_id, link.run_id)
        page = self._events(link, sequence)
        return tuple(
            project_run_event(event, run_state=snapshot.state.value)
            for event in page.events
        )

    @staticmethod
    def _check_handle(link, handle):
        if (
            link.run_id is None
            or handle.adapter_session_id != f"agent-run:{link.run_id}"
            or handle.execution_session_id
            != uuid5(NAMESPACE_URL, f"sidera.native.session:{link.run_id}")
        ):
            raise AgentError(
                "permission_denied", "Original native Session is unavailable."
            )

    def stop_session(self, handle, *, operation_id):
        require_gateway_owner(self._owner_check)
        link = self._canonical()
        self._check_handle(link, handle)
        self._authorizer.require(link.authority_id, "run.stop", link.workspace_id)
        require_gateway_owner(self._owner_check)
        receipt = self._execution.stop(
            RunStop(
                workspace_id=link.workspace_id,
                run_id=link.run_id,
                operation_id=operation_id,
                grant_id=link.authority_id,
            )
        )
        if receipt.run_id != link.run_id or receipt.operation_id != operation_id:
            raise AgentError("configuration_mismatch", "Native stop identity changed.")
        delivered = (
            receipt.status.value == "stopped" and receipt.evidence_sequence is not None
        )
        return DeliveryResult(
            operation_id,
            "delivered" if delivered else "unknown",
            receipt.evidence_sequence if delivered else None,
            receipt.reason_code,
            handle.execution_session_id,
        )

    def send_input(self, handle, text, attachment_refs, *, operation_id):
        require_gateway_owner(self._owner_check)
        link = self._canonical()
        self._check_handle(link, handle)
        self._authorizer.require(link.authority_id, "run.input", link.workspace_id)
        snapshot = self.get_run(link.workspace_id, link.run_id)
        if not snapshot.input_open or snapshot.state.value not in {"running", "idle"}:
            raise AgentError("native_input_closed", "Original native input is closed.")
        with self._uow_factory() as uow:
            binding = native_binding(
                uow, uow.executions.get(link.execution_id, link.workspace_id)
            )
        request = self._builder.build_input(
            link.execution_id,
            binding,
            link.authority_id,
            operation_id=operation_id,
            text=text,
            attachment_refs=attachment_refs,
        )
        self._authorizer.require(link.authority_id, "run.input", link.workspace_id)
        require_gateway_owner(self._owner_check)
        try:
            return self._input_result(
                link, operation_id, self._execution.send_input(request)
            )
        except Exception:  # noqa: BLE001 - post-call failures cannot prove non-admission
            # An exception after the public write cannot prove non-admission.
            return DeliveryResult(
                operation_id,
                DeliveryOutcome.UNKNOWN,
                None,
                "native_input_admission_unconfirmed",
            )

    def get_input(self, workspace_id, run_id, operation_id):
        if workspace_id != self._workspace_id:
            raise AgentError(
                "permission_denied", "Original native input is unavailable."
            )
        link = self._canonical(run_id=run_id)
        with self._uow_factory() as uow:
            item = uow.execution_inputs.get_by_operation_id(
                operation_id, workspace_id, self._execution_id
            )
            session = (
                None
                if item is None
                else uow.sessions.get_any(item.session_id, workspace_id)
            )
            if (
                item is None
                or session is None
                or item.operation_id != input_operation_id(item.id)
                or session.execution_id != link.execution_id
                or session.adapter_session_id != f"agent-run:{link.run_id}"
            ):
                raise AgentError(
                    "permission_denied", "Original native input is unavailable."
                )
        self._authorizer.require(link.authority_id, "run.read", link.workspace_id)
        receipt = self._execution.get_input(
            workspace_id, run_id, operation_id, grant_id=link.authority_id
        )
        return self._input_result(link, operation_id, receipt)

    @staticmethod
    def _input_result(link, operation_id, receipt):
        if receipt.run_id != link.run_id or receipt.operation_id != operation_id:
            raise AgentError("configuration_mismatch", "Native input identity changed.")
        status = receipt.status
        outcomes = {
            InputStatus.ACCEPTED: DeliveryOutcome.ACCEPTED,
            InputStatus.DELIVERED: DeliveryOutcome.DELIVERED,
            InputStatus.FAILED: DeliveryOutcome.FAILED,
            InputStatus.REJECTED: DeliveryOutcome.FAILED,
            InputStatus.UNKNOWN: DeliveryOutcome.UNKNOWN,
        }
        if status is InputStatus.DELIVERED and (
            type(receipt.evidence_sequence) is not int or receipt.evidence_sequence < 1
        ):
            raise AgentError(
                "native_input_evidence_missing", "Native delivery is unconfirmed."
            )
        return DeliveryResult(
            operation_id,
            outcomes[status],
            receipt.evidence_sequence if status is InputStatus.DELIVERED else None,
            receipt.reason_code,
        )

    def collect_artifacts(self, handle, *, operation_id):
        self._check_handle(self._canonical(), handle)
        return ()

    def collect_knowledge_candidates(self, handle, *, operation_id, **kwargs):
        self._check_handle(self._canonical(), handle)
        return ()
