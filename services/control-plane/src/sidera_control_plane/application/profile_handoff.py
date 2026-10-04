"""Native assignee translation for the existing audited handoff lifecycle."""

import json
from collections.abc import Mapping
from contextlib import nullcontext
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError as ContractValidationError
from sidera_agents.contracts import ProfileRef, RunStop, StopReceipt
from sidera_agents.errors import AgentError

from sidera_control_plane.application.agent_worker_adapter import (
    NativeContextPreparationReceipt,
    read_native_preparation_receipt,
    require_gateway_owner,
)
from sidera_control_plane.application.assignments import (
    AssignmentCommandHandler,
    TrustedAssignmentIdentity,
    require_trusted_assignment_identity,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
)
from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    require_handoff_operation,
)
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    _gateway_event,
)
from sidera_control_plane.domain.agent_bindings import (
    AgentBinding,
    AssigneeRef,
    AssignmentDenied,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    DomainError,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffStopEvidence,
    HandoffWorkspaceTargetSpec,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    NativeHandoffProposalPayload,
)
from sidera_control_plane.domain.workspace import (
    workspace_location_ref_hash,
    workspace_manifest_digest,
)


def native_handoff_preparation(uow, transition, *, now):
    """Validate current CP pins without assembling or reading source bodies."""
    payload = transition.native_payload
    link = uow.execution_authorities.get_link(
        transition.workspace_id, transition.target_execution_id
    )
    if (
        transition.handoff_schema_version != 2
        or payload is None
        or link is None
        or (
            link.profile_ref != payload.target_profile_ref
            or link.binding_version != transition.target_binding_version
            or link.run_id is None
            or link.start_receipt is None
            or link.start_receipt.status.value != "accepted"
            or link.start_receipt.run_id != link.run_id
            or link.start_receipt.operation_id != link.start_operation_id
            or link.status not in {"starting", "running", "waiting"}
        )
    ):
        raise GatewayMessageError(
            "native target start proof changed", code="native_handoff_start_invalid"
        )
    execution, lease, link, authority = uow.execution_authorities.lock_intention(
        link, now=now
    )
    receipt = read_native_preparation_receipt(uow, link)
    package = uow.context_packages.get(
        receipt.package_id, transition.workspace_id, for_update=True
    )
    revision = uow.work_contexts.get_revision(
        receipt.revision_id, transition.workspace_id
    )
    item = uow.worker_items.get(
        transition.worker_item_id, transition.workspace_id, for_update=True
    )
    context = (
        None
        if item is None
        else uow.work_contexts.get_by_root_task(
            item.root_task_id, transition.workspace_id
        )
    )
    manifest = uow.workspaces.get(
        receipt.workspace_manifest_id, transition.workspace_id, for_update=True
    )
    session = uow.sessions.get_any(
        transition.target_session_id, transition.workspace_id, for_update=True
    )
    if (
        package is None
        or revision is None
        or item is None
        or context is None
        or manifest is None
        or session is None
        or (
            execution.id != transition.target_execution_id
            or execution.worker_item_id != transition.worker_item_id
            or execution.status.value != "running"
            or execution.stop_requested
            or session.execution_id != execution.id
            or session.adapter_session_id != f"agent-run:{link.run_id}"
            or session.status.value != "active"
            or session.worker_id != lease.worker_id
            or receipt.package_id != transition.target_context_package_id
            or receipt.package_hash != transition.target_context_package_hash
            or package.package_hash != receipt.package_hash
            or package.consumer_id != execution.id
            or package.worker_item_id != item.id
            or package.status.value != "active"
            or package.expires_at <= now
            or receipt.context_version != transition.target_context_version
            or receipt.context_version != item.context_version
            or receipt.context_version != package.context_version
            or receipt.revision_id != package.work_context_revision_id
            or receipt.revision_id != context.current_revision_id
            or receipt.revision_hash != revision.content_hash
            or receipt.workspace_manifest_id != transition.target_workspace_manifest_id
            or receipt.workspace_manifest_digest != workspace_manifest_digest(manifest)
            or manifest.lifecycle.value != "active"
            or receipt.fencing_token != manifest.fencing_token
            or receipt.lease_id != lease.id
            or not receipt.sources
            or any(source.ref not in authority.sources for source in receipt.sources)
        )
    ):
        raise GatewayMessageError(
            "native target preparation changed", code="context_package_stale"
        )
    transfer = uow.workspace_transfers.get_by_attempt(
        transition.id, transition.logical_attempt, transition.workspace_id
    )
    if transfer is None or (
        transfer.status.value != "succeeded"
        or transfer.source_manifest_id != transition.source_workspace_manifest_id
        or transfer.source_execution_id != transition.source_execution_id
        or transfer.target_execution_id != execution.id
        or transfer.target_manifest_id != manifest.id
        or transfer.location_ref_hash
        != workspace_location_ref_hash(manifest.location_ref)
        or receipt.workspace_location_hash != transfer.location_ref_hash
        or manifest.fencing_token < transfer.target_fencing_token
    ):
        raise GatewayMessageError(
            "native target transfer proof changed",
            code="native_handoff_workspace_changed",
        )
    retired = {
        pin.fencing_token: pin
        for pin in uow.execution_authorities.admission_history(
            transition.workspace_id, execution.id
        )
    }
    for fence in range(transfer.target_fencing_token, manifest.fencing_token):
        pin = retired.get(fence)
        if (
            pin is None
            or pin.workspace_snapshot is None
            or pin.execution_binding_snapshot is None
            or (
                not pin.link.not_admitted
                or pin.link.profile_ref != link.profile_ref
                or pin.link.binding_version != link.binding_version
                or pin.workspace_snapshot.get("id") != str(manifest.id)
                or pin.workspace_snapshot.get("fencing_token") != fence
                or pin.workspace_snapshot.get("location_ref") != manifest.location_ref
            )
        ):
            raise GatewayMessageError(
                "native target refence proof missing",
                code="runtime_binding_version_mismatch",
            )
    return link, receipt


def native_handoff_confirmation_pins(link, receipt):
    return {
        "schema_version": 2,
        "run_id": str(link.run_id),
        "start_operation_id": str(link.start_operation_id),
        "profile_ref": link.profile_ref.model_dump(mode="json"),
        "preparation_command_id": str(receipt.command_id),
        "preparation_sha256": sha256(
            json.dumps(
                receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }


def validate_native_handoff_confirmation(uow, transition, *, now):
    link, receipt = native_handoff_preparation(uow, transition, now=now)
    operation_id = handoff_step_operation_id(
        transition.id,
        HandoffPhase.ACKNOWLEDGE_CONTEXT,
        logical_attempt=transition.logical_attempt,
        action="enable_writer",
    )
    operation = uow.gateway_operations.get_by_operation_id(
        operation_id, transition.workspace_id, for_update=True
    )
    if (
        operation is None
        or operation.status is not GatewayOperationStatus.SUCCEEDED
        or (
            operation.execution_id != transition.target_execution_id
            or operation.session_id != transition.target_session_id
            or operation.lease_id != receipt.lease_id
            or operation.fencing_token != receipt.fencing_token
            or operation.operation_type
            is not GatewayOperationType.HANDOFF_ENABLE_WRITER
        )
    ):
        raise GatewayMessageError(
            "native target confirmation missing",
            code="native_handoff_confirmation_missing",
        )
    try:
        proof = json.loads(operation.result_ref)
    except (TypeError, ValueError):
        proof = None
    pins = native_handoff_confirmation_pins(link, receipt)
    if (
        not isinstance(proof, dict)
        or set(proof) != set(pins) | {"started_sequence"}
        or any(proof.get(key) != value for key, value in pins.items())
        or type(proof.get("started_sequence")) is not int
        or proof["started_sequence"] < 1
    ):
        raise GatewayMessageError(
            "native target confirmation changed",
            code="native_handoff_confirmation_invalid",
        )
    session = uow.sessions.get_any(
        transition.target_session_id, transition.workspace_id
    )
    if session is None or proof["started_sequence"] > session.last_sequence:
        raise GatewayMessageError(
            "native started observation missing", code="native_handoff_start_invalid"
        )
    return proof


NATIVE_PREPARE_KEYS = frozenset(
    {
        "proposal_schema_version",
        "context",
        "target_assignee",
        "target_profile_ref",
        "handoff_reason",
        "handoff_instruction",
        "input_plan",
        "proposal_ttl_seconds",
    }
)
NATIVE_CONTEXT_KEYS = frozenset(
    {
        "execution_mode",
        "worker_item_id",
        "plan_generation",
        "context_version",
        "execution_id",
        "session_id",
        "session_version",
        "worker_id",
        "binding_version",
        "run_id",
    }
)


class ProfileHandoffValidator:
    def __init__(self, *, assignments, identity, native_services, clock):
        self.assignments, self.identity, self.services, self.clock = (
            assignments,
            identity,
            native_services,
            clock,
        )

    def preflight(self, uow, envelope):
        if envelope.command_type not in {
            "PrepareProfileHandoff",
            "ConfirmProfileHandoff",
        }:
            return
        require_trusted_assignment_identity(envelope, self.identity)
        if (
            envelope.command_type == "ConfirmProfileHandoff"
            and envelope.actor_type != "user"
        ):
            raise AssignmentDenied(
                "native handoff confirmation requires a trusted Web human"
            )
        if self.services is None:
            raise AssignmentDenied("native handoff services required")
        if envelope.command_type == "PrepareProfileHandoff":
            if (
                envelope.target_type != "worker_item"
                or type(envelope.target_id) is not UUID
            ):
                raise ValidationError("native prepare targets a WorkerItem")
            self.assignments.authorize_target(
                envelope.workspace_id, "worker_item", envelope.target_id
            )
            address = self.address(envelope.payload)
            if address["worker_item_id"] != envelope.target_id:
                raise AssignmentDenied("native handoff WorkerItem scope mismatch")
            self.original(uow, envelope.workspace_id, address)
        else:
            if (
                envelope.target_type != "handoff_proposal"
                or type(envelope.target_id) is not UUID
            ):
                raise ValidationError("native confirm targets a proposal")
            proposal = uow.handoff_proposals.get(
                envelope.target_id, envelope.workspace_id
            )
            if proposal is None or not isinstance(
                proposal.payload, NativeHandoffProposalPayload
            ):
                raise AssignmentDenied("original native proposal unavailable")
            self.authorize_payload(uow, proposal.payload)

    def payload_address(self, payload):
        return {
            "worker_item_id": payload.worker_item_id,
            "execution_id": payload.source_execution_id,
            "session_id": payload.source_session_id,
            "worker_id": payload.source_worker_id,
            "run_id": payload.source_run_id,
            "binding_version": payload.source_binding_version,
            "session_version": payload.source_session_version,
            "context_version": payload.impact_summary["worker_item_context_version"],
            "plan_generation": payload.observed_plan_generation,
        }

    def authorize_payload(self, uow, payload):
        if self.identity is None or self.services is None:
            raise AssignmentDenied("trusted native handoff services required")
        self.assignments.authorize_target(
            payload.workspace_id, "worker_item", payload.worker_item_id
        )
        _, _, link, authority = self.original(
            uow, payload.workspace_id, self.payload_address(payload)
        )
        if (
            authority.id != payload.source_authority_id
            or link.profile_ref != payload.source_profile_ref
            or (authority.target_type, authority.target_id)
            != (payload.source_binding_target_type, payload.source_binding_target_id)
        ):
            raise AssignmentDenied("original native handoff proposal pins changed")

    def preflight_cancel(self, uow, envelope, payload):
        require_trusted_assignment_identity(envelope, self.identity)
        self.authorize_payload(uow, payload)

    def validate_payload(self, uow, payload):
        self.authorize_payload(uow, payload)
        arguments = {
            "reason": payload.handoff_reason,
            "instruction": payload.handoff_instruction,
            "input_plan": payload.input_plan,
            "expected_version": payload.source_worker_item_version,
        }
        address = self.payload_address(payload)
        before = self.collect(
            uow,
            payload.workspace_id,
            address,
            payload.target_assignee,
            payload.target_profile_ref,
            **arguments,
        )
        if before != payload:
            raise ValidationError("native handoff proposal source is stale")
        self.target(
            payload.workspace_id,
            payload.target_assignee,
            payload.target_profile_ref,
            frozenset(before.impact_summary["capability_requirements"]),
        )
        reader = self.reader(uow)
        reader.require(payload.source_authority_id, "run.read", payload.workspace_id)
        snapshot = self.services.execution.get_run(
            payload.workspace_id,
            payload.source_run_id,
            grant_id=payload.source_authority_id,
        )
        if (snapshot.workspace_id, snapshot.run_id, snapshot.profile_ref) != (
            payload.workspace_id,
            payload.source_run_id,
            payload.source_profile_ref,
        ) or snapshot.state.value not in {"running", "idle"}:
            raise AssignmentDenied("native handoff source Run is no longer active")
        reader.require(payload.source_authority_id, "run.read", payload.workspace_id)
        self.target(
            payload.workspace_id,
            payload.target_assignee,
            payload.target_profile_ref,
            frozenset(before.impact_summary["capability_requirements"]),
        )
        self.authorize_payload(uow, payload)
        after = self.collect(
            uow,
            payload.workspace_id,
            address,
            payload.target_assignee,
            payload.target_profile_ref,
            lock=True,
            **arguments,
        )
        if after != payload:
            raise ValidationError("native handoff proposal changed during inspection")

    def stale_reason(self, uow, payload):
        try:
            self.validate_payload(uow, payload)
        except (DomainError, AgentError) as error:
            return error.code
        return None

    def address(self, payload):
        address = payload.get("context")
        if not isinstance(address, Mapping) or set(address) != NATIVE_CONTEXT_KEYS:
            raise ValidationError("exact native handoff context required")
        if address["execution_mode"] != "native_profile":
            raise ValidationError("native handoff context required")
        values = {}
        for key, value in address.items():
            if key.endswith("_id"):
                try:
                    values[key] = UUID(str(value))
                except (TypeError, ValueError):
                    raise ValidationError("invalid native context identity") from None
            elif key != "execution_mode":
                if type(value) is not int or value < 1:
                    raise ValidationError("native context version must be positive")
                values[key] = value
        return values

    def original(self, uow, workspace_id, address, *, lock=False):
        execution = uow.executions.get(
            address["execution_id"], workspace_id, for_update=lock
        )
        session = uow.sessions.get_any(
            address["session_id"], workspace_id, for_update=lock
        )
        link = uow.execution_authorities.get_link(workspace_id, address["execution_id"])
        authority = (
            None
            if link is None
            else uow.execution_authorities.get(
                link.authority_id, workspace_id=workspace_id
            )
        )
        if (
            execution is None
            or session is None
            or link is None
            or authority is None
            or execution.worker_item_id != address["worker_item_id"]
            or session.execution_id != execution.id
            or session.worker_id != address["worker_id"]
            or session.adapter_session_id != f"agent-run:{link.run_id}"
            or link.run_id != address["run_id"]
            or link.binding_version != address["binding_version"]
            or authority.execution_id != execution.id
            or authority.profile_ref != link.profile_ref
            or authority.binding_version != link.binding_version
            or authority.principal_id != self.identity.principal_id
        ):
            raise AssignmentDenied("original native handoff association unavailable")
        historical = next(
            (
                binding
                for binding in uow.agent_bindings.history(
                    workspace_id, authority.target_type, authority.target_id
                )
                if binding.version == link.binding_version
            ),
            None,
        )
        if historical is None or historical.profile_ref != link.profile_ref:
            raise AssignmentDenied("original native handoff binding unavailable")
        return execution, session, link, authority

    def reader(self, uow):
        return ExecutionAuthorityReader(
            lambda: nullcontext(uow),
            principal=lambda: self.identity.principal_id,
            authorizer=self.services.authorizer,
            clock=self.clock,
        )

    def target(self, workspace_id, assignee, profile_ref, requirements):
        if assignee.kind == "human":
            if (
                str(assignee.id) != self.identity.principal_id
                or profile_ref is not None
            ):
                raise AssignmentDenied(
                    "human target must be the current trusted local user"
                )
            return
        if (
            profile_ref is None
            or profile_ref.workspace_id != workspace_id
            or (profile_ref.profile_id != assignee.id)
        ):
            raise AssignmentDenied("exact target Profile required")
        pin, reasons = self.assignments.resolve_profile(
            workspace_id, profile_ref.profile_id, profile_ref.revision, requirements
        )
        if pin != profile_ref or reasons:
            raise AssignmentDenied("target Profile is not runnable")

    def prepare_payload(self, uow, envelope, *, max_proposal_ttl_seconds):
        payload = envelope.payload
        if (
            set(payload) != NATIVE_PREPARE_KEYS
            or type(payload["proposal_schema_version"]) is not int
            or (payload["proposal_schema_version"] != 2)
        ):
            raise ValidationError("exact schema-2 prepare payload required")
        if (
            type(payload["proposal_ttl_seconds"]) is not int
            or not 1 <= payload["proposal_ttl_seconds"] <= max_proposal_ttl_seconds
        ):
            raise ValidationError("native proposal TTL is invalid")
        raw = payload["target_assignee"]
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "id"}:
            raise ValidationError("exact target assignee required")
        assignee = AssigneeRef(raw["kind"], raw["id"])
        profile = (
            None
            if payload["target_profile_ref"] is None
            else ProfileRef.model_validate(payload["target_profile_ref"])
        )
        address = self.address(payload)
        before = self.collect(
            uow,
            envelope.workspace_id,
            address,
            assignee,
            profile,
            reason=payload["handoff_reason"],
            instruction=payload["handoff_instruction"],
            input_plan=payload["input_plan"],
            expected_version=envelope.expected_version,
        )
        self.target(
            envelope.workspace_id,
            assignee,
            profile,
            frozenset(before.impact_summary["capability_requirements"]),
        )
        reader = self.reader(uow)
        reader.require(before.source_authority_id, "run.read", before.workspace_id)
        snapshot = self.services.execution.get_run(
            before.workspace_id,
            before.source_run_id,
            grant_id=before.source_authority_id,
        )
        if (snapshot.workspace_id, snapshot.run_id, snapshot.profile_ref) != (
            before.workspace_id,
            before.source_run_id,
            before.source_profile_ref,
        ) or snapshot.state.value not in {"running", "idle"}:
            raise AssignmentDenied("original native source is not active")
        reader.require(before.source_authority_id, "run.read", before.workspace_id)
        self.target(
            envelope.workspace_id,
            assignee,
            profile,
            frozenset(before.impact_summary["capability_requirements"]),
        )
        self.assignments.authorize_target(
            envelope.workspace_id, "worker_item", envelope.target_id
        )
        after = self.collect(
            uow,
            envelope.workspace_id,
            address,
            assignee,
            profile,
            reason=payload["handoff_reason"],
            instruction=payload["handoff_instruction"],
            input_plan=payload["input_plan"],
            expected_version=envelope.expected_version,
            lock=True,
        )
        if before != after:
            raise ValidationError("native handoff source changed during inspection")
        return after

    def collect(
        self,
        uow,
        workspace_id,
        address,
        assignee,
        profile,
        *,
        reason,
        instruction,
        input_plan,
        expected_version,
        lock=False,
    ):
        execution, session, link, authority = self.original(
            uow, workspace_id, address, lock=lock
        )
        item = uow.worker_items.get(
            execution.worker_item_id, workspace_id, for_update=lock
        )
        placement = uow.worker_items.get_current_placement(
            execution.worker_item_id, workspace_id
        )
        if item is None or placement is None:
            raise AssignmentDenied("native source placement unavailable")
        if type(expected_version) is not int or expected_version < 1:
            raise ValidationError("WorkerItem version required")
        if item.version != expected_version:
            raise VersionConflict(expected_version, item.version)
        head = uow.plans.get_head(item.root_task_id, workspace_id, for_update=lock)
        current = uow.agent_bindings.get(
            workspace_id, authority.target_type, authority.target_id, for_update=lock
        )
        lease = uow.leases.get_active_for_execution(
            execution.id, workspace_id, for_update=lock
        )
        binding = uow.bindings.get_for_execution(
            execution.id, workspace_id, for_update=lock
        )
        manifest = uow.workspaces.get_for_execution(
            execution.id, workspace_id, for_update=lock
        )
        worker = uow.workers.get(session.worker_id, workspace_id, for_update=lock)
        if (
            item.status.value != "active"
            or execution.status.value != "running"
            or session.status.value != "active"
            or session.version != address["session_version"]
            or item.context_version != address["context_version"]
            or head is None
            or head.active_snapshot_id != placement.plan_snapshot_id
            or head.active_generation != address["plan_generation"]
            or execution.task_id != placement.subtask_task_id
            or current is None
            or current.version != link.binding_version
            or current.profile_ref != link.profile_ref
            or authority.revoked
            or lease is None
            or lease.expires_at <= self.clock()
            or lease.id != authority.lease_id
            or lease.fencing_token != authority.fencing_token
            or execution.active_lease_id != lease.id
            or execution.assigned_worker_id != session.worker_id
            or binding is None
            or binding.lease_id != lease.id
            or binding.worker_id != session.worker_id
            or binding.fencing_token != lease.fencing_token
            or manifest is None
            or manifest.id != authority.workspace_ref
            or manifest.lifecycle.value != "active"
            or manifest.fencing_token != lease.fencing_token
            or worker is None
            or uow.handoff_transitions.get_active_for_worker_item(item.id, workspace_id)
            is not None
            or any(
                t.status.value == "converging"
                for t in uow.plan_transitions.list_for_root(
                    item.root_task_id, workspace_id
                )
            )
        ):
            raise AssignmentDenied("native handoff source or plan changed")
        prepared = NativeContextPreparationReceipt.model_validate(link.preparation)
        package = uow.context_packages.get(
            prepared.package_id, workspace_id, for_update=lock
        )
        context = uow.work_contexts.get_by_root_task(
            item.root_task_id, workspace_id, for_update=lock
        )
        revision = (
            None
            if context is None
            else uow.work_contexts.get_revision(
                context.current_revision_id, workspace_id, for_update=lock
            )
        )
        if (
            package is None
            or prepared.workspace_id != workspace_id
            or prepared.execution_id != execution.id
            or prepared.authority_id != authority.id
            or prepared.binding_version != link.binding_version
            or prepared.lease_id != lease.id
            or prepared.fencing_token != lease.fencing_token
            or package.package_hash != prepared.package_hash
            or package.consumer_id != execution.id
            or package.worker_item_id != item.id
            or package.status.value != "active"
            or context is None
            or revision is None
            or revision.work_context_id != context.id
            or revision.content_hash != revision.snapshot.content_hash
        ):
            raise AssignmentDenied("native source/context pins unavailable")
        return NativeHandoffProposalPayload(
            proposal_schema_version=2,
            workspace_id=workspace_id,
            root_task_id=item.root_task_id,
            subtask_id=execution.task_id,
            worker_item_id=item.id,
            source_worker_item_version=item.version,
            source_execution_id=execution.id,
            source_execution_version=execution.version,
            source_session_id=session.id,
            source_session_version=session.version,
            source_session_last_sequence=session.last_sequence,
            source_worker_id=worker.id,
            source_worker_version=worker.version,
            source_workspace_manifest_id=manifest.id,
            source_workspace_manifest_version=manifest.version,
            source_context_package_id=package.package_id,
            source_context_package_hash=package.package_hash,
            source_worker_context_version=package.context_version,
            observed_plan_snapshot_id=head.active_snapshot_id,
            observed_plan_generation=head.active_generation,
            observed_plan_head_version=head.version,
            source_run_id=link.run_id,
            source_authority_id=authority.id,
            source_binding_version=link.binding_version,
            source_profile_ref=link.profile_ref,
            source_binding_target_type=authority.target_type,
            source_binding_target_id=authority.target_id,
            work_context_id=context.id,
            work_context_revision_id=revision.revision_id,
            work_context_hash=revision.content_hash,
            work_context_version=context.version,
            target_assignee=assignee,
            target_profile_ref=profile,
            handoff_reason=reason,
            handoff_instruction=instruction,
            input_plan=input_plan,
            impact_summary={
                "worker_item_context_version": item.context_version,
                "capability_requirements": sorted(execution.capability_requirements),
            },
        )


class ProfileHandoffPhaseService:
    """Translate native public facts; the existing Coordinator owns phase changes."""

    def __init__(self, uow_factory, *, native_services, clock, owner_check=None):
        self._uow_factory = uow_factory
        self._services = native_services
        self._clock = clock
        self._owner_check = owner_check
        self._reader = ExecutionAuthorityReader(
            uow_factory,
            principal=lambda: native_services.principal_id,
            authorizer=native_services.authorizer,
            clock=clock,
        )
        self._profiles = AssignmentCommandHandler(
            catalog=native_services.catalog,
            runtimes=native_services.runtimes,
            identity=TrustedAssignmentIdentity(
                native_services.principal_id, native_services.catalog_grant_id
            ),
            clock=clock,
        )

    def prepare_assignment(self, transition):
        with self._uow_factory() as uow:
            current = self._current(
                uow, transition, HandoffGateOperation.COORDINATOR_CREATE_TARGET
            )
            facts = self._assignment_facts(uow, current)
        payload = current.native_payload
        if payload.target_assignee.kind == "human":
            if (
                str(payload.target_assignee.id) != self._services.principal_id
                or current.confirmed_by_id != self._services.principal_id
            ):
                raise GatewayMessageError(
                    "trusted human assignment changed",
                    code="native_handoff_target_changed",
                )
        else:
            pin, reasons = self._profiles.resolve_profile(
                current.workspace_id,
                payload.target_assignee.id,
                payload.target_profile_ref.revision,
                frozenset(payload.impact_summary["capability_requirements"]),
            )
            if pin != payload.target_profile_ref or reasons:
                raise GatewayMessageError(
                    "native target Profile unavailable",
                    code="native_handoff_target_changed",
                )
        return facts

    def workspace_target(self, transition, manifest):
        with self._uow_factory() as uow:
            current = self._current(
                uow, transition, HandoffGateOperation.COORDINATOR_TRANSFER_WORKSPACE
            )
            binding = uow.bindings.get_for_execution(
                current.source_execution_id, current.workspace_id
            )
            if binding is None:
                raise GatewayMessageError(
                    "original workspace provider unavailable",
                    code="native_handoff_workspace_changed",
                )
            return HandoffWorkspaceTargetSpec(
                workspace_id=current.workspace_id,
                task_id=manifest.task_id,
                target_execution_id=current.target_execution_id,
                target_worker_id=None,
                adapter_module_ref=None,
                adapter_config_version=None,
                workspace_module_ref=binding.workspace_module_id,
                workspace_config_version=binding.workspace_config_version,
                repositories=tuple(
                    {
                        "repository_ref": checkout.repository_ref,
                        "base_revision": checkout.base_revision,
                        "branch": checkout.branch,
                        "current_head": checkout.current_head,
                    }
                    for checkout in manifest.checkouts
                ),
                anticipated_fencing_token=manifest.fencing_token + 1,
                schema_version=2,
                target_profile_ref=current.native_payload.target_profile_ref,
                target_binding_version=current.target_binding_version,
            )

    def _assignment_facts(self, uow, current, *, lock=False):
        payload = current.native_payload
        execution, session, _, manifest = self._source(uow, current, lock=lock)
        checkpoint = HandoffCheckpointEvidence.from_native_mapping(
            current.source_checkpoint_facts
        )
        stop = HandoffStopEvidence.from_native_mapping(current.source_stop_facts)
        stop.assert_matches(checkpoint)
        frozen = current.source_workspace_freeze_facts
        item = uow.worker_items.get(
            current.worker_item_id, current.workspace_id, for_update=lock
        )
        placement = uow.worker_items.get_current_placement(
            current.worker_item_id, current.workspace_id
        )
        head = uow.plans.get_head(
            payload.root_task_id, current.workspace_id, for_update=lock
        )
        context = uow.work_contexts.get_by_root_task(
            payload.root_task_id, current.workspace_id, for_update=lock
        )
        revision = (
            None
            if context is None
            else uow.work_contexts.get_revision(
                context.current_revision_id, current.workspace_id, for_update=lock
            )
        )
        original = uow.agent_bindings.get(
            current.workspace_id,
            payload.source_binding_target_type,
            payload.source_binding_target_id,
            for_update=lock,
        )
        if (
            item is None
            or placement is None
            or head is None
            or context is None
            or revision is None
            or original is None
            or frozen is None
            or (
                execution.status.value != "canceled"
                or session.status.value != "closed"
                or manifest.lifecycle.value != "frozen"
                or frozen.get("workspace_digest") != workspace_manifest_digest(manifest)
                or frozen.get("manifest_id") != str(manifest.id)
                or frozen.get("stop_sequence") != stop.accepted_sequence
                or original.version != payload.source_binding_version
                or original.profile_ref != payload.source_profile_ref
                or item.status.value != "active"
                or item.version != payload.source_worker_item_version
                or (
                    placement.plan_snapshot_id,
                    placement.plan_generation,
                    placement.subtask_task_id,
                )
                != (
                    payload.observed_plan_snapshot_id,
                    payload.observed_plan_generation,
                    payload.subtask_id,
                )
                or (head.active_snapshot_id, head.active_generation, head.version)
                != (
                    payload.observed_plan_snapshot_id,
                    payload.observed_plan_generation,
                    payload.observed_plan_head_version,
                )
                or context.id != payload.work_context_id
                or revision.work_context_id != context.id
                or revision.content_hash != revision.snapshot.content_hash
            )
        ):
            raise GatewayMessageError(
                "native assignment source or context changed",
                code="native_handoff_assignment_stale",
            )
        inputs = uow.execution_inputs.list_for_session(
            session.id, current.workspace_id, for_update=lock
        )
        results = tuple(
            result
            for result in uow.results.retained_code_results(current.workspace_id)
            if result.worker_item_id == item.id
        )
        return {
            "schema_version": 2,
            "workspace_id": str(current.workspace_id),
            "worker_item_id": str(item.id),
            "worker_item_version": item.version,
            "context_version": item.context_version,
            "plan_snapshot_id": str(head.active_snapshot_id),
            "plan_generation": head.active_generation,
            "plan_head_version": head.version,
            "work_context_id": str(context.id),
            "work_context_revision_id": str(revision.revision_id),
            "work_context_hash": revision.content_hash,
            "work_context_version": context.version,
            "approved_goal": revision.snapshot.objective,
            "handoff_instruction": payload.handoff_instruction,
            "source_execution_id": str(execution.id),
            "source_session_id": str(session.id),
            "source_context_package_id": str(payload.source_context_package_id),
            "source_context_package_hash": payload.source_context_package_hash,
            "source_result_refs": [f"code-result:{result.id}" for result in results],
            "source_input_refs": [str(item.id) for item in inputs],
            "source_input_dispositions": [
                {
                    "input_id": str(item.id),
                    "version": item.version,
                    "status": item.status.value,
                    "operation_id": None
                    if item.operation_id is None
                    else str(item.operation_id),
                    "disposition": "manual_retained"
                    if payload.target_assignee.kind == "human"
                    else "source_history",
                }
                for item in inputs
            ],
        }

    def assign_in_uow(self, uow, transition, expected):
        current = self._current(
            uow, transition, HandoffGateOperation.COORDINATOR_CREATE_TARGET, lock=True
        )
        facts = self._assignment_facts(uow, current, lock=True)
        if facts != expected:
            raise GatewayMessageError(
                "native assignment changed during target inspection",
                code="native_handoff_assignment_stale",
            )
        payload = current.native_payload
        item = uow.worker_items.get(
            current.worker_item_id, current.workspace_id, for_update=True
        )
        old = uow.agent_bindings.get(
            current.workspace_id, "worker_item", item.id, for_update=True
        )
        now = self._clock()
        binding = AgentBinding(
            workspace_id=current.workspace_id,
            target_type="worker_item",
            target_id=item.id,
            assignee=payload.target_assignee,
            profile_ref=payload.target_profile_ref,
            version=1 if old is None else old.version + 1,
            created_at=now if old is None else old.created_at,
            changed_at=now,
            runnable=payload.target_assignee.kind == "profile",
        )
        uow.agent_bindings.append(binding)
        previous = item.version
        item.version += 1
        uow.worker_items.save(item, previous)
        uow.execution_authorities.revoke(
            payload.source_authority_id, current.workspace_id
        )
        previous = current.version
        current.target_binding_version = binding.version
        current.target_context_version = item.context_version
        current.version += 1
        uow.handoff_transitions.save(current, previous)
        transition.target_binding_version = current.target_binding_version
        transition.target_context_version = current.target_context_version
        transition.version = current.version
        return facts

    def _current(self, uow, transition, operation, *, lock=False):
        current = uow.handoff_transitions.get(
            transition.id, transition.workspace_id, for_update=lock
        )
        if (
            current is None
            or current.handoff_schema_version != 2
            or current.native_payload is None
        ):
            raise GatewayMessageError(
                "original native transition unavailable",
                code="native_handoff_scope_changed",
            )
        if (
            current.phase is not transition.phase
            or current.logical_attempt != transition.logical_attempt
            or (
                current.lease_owner != transition.lease_owner
                or current.lease_expires_at is None
                or current.lease_expires_at <= self._clock()
            )
        ):
            raise GatewayMessageError(
                "native phase ownership changed", code="native_handoff_claim_changed"
            )
        require_handoff_operation(
            uow,
            worker_item_id=current.worker_item_id,
            workspace_id=current.workspace_id,
            operation=operation,
            transition_id=current.id,
            target_execution_id=current.target_execution_id,
        )
        return current

    def _source(self, uow, current, *, lock=False):
        payload = current.native_payload
        execution = uow.executions.get(
            current.source_execution_id, current.workspace_id, for_update=lock
        )
        session = uow.sessions.get_any(
            current.source_session_id, current.workspace_id, for_update=lock
        )
        link = uow.execution_authorities.get_link(
            current.workspace_id, current.source_execution_id
        )
        authority = uow.execution_authorities.get(
            payload.source_authority_id, workspace_id=current.workspace_id
        )
        manifest = uow.workspaces.get(
            current.source_workspace_manifest_id, current.workspace_id, for_update=lock
        )
        if (
            execution is None
            or session is None
            or link is None
            or authority is None
            or manifest is None
            or (
                execution.worker_item_id != current.worker_item_id
                or session.execution_id != execution.id
                or session.worker_id != payload.source_worker_id
                or session.adapter_session_id != f"agent-run:{payload.source_run_id}"
                or (
                    link.run_id,
                    link.authority_id,
                    link.binding_version,
                    link.profile_ref,
                )
                != (
                    payload.source_run_id,
                    payload.source_authority_id,
                    payload.source_binding_version,
                    payload.source_profile_ref,
                )
                or (
                    authority.execution_id,
                    authority.binding_version,
                    authority.profile_ref,
                    authority.principal_id,
                )
                != (
                    execution.id,
                    payload.source_binding_version,
                    payload.source_profile_ref,
                    self._services.principal_id,
                )
                or manifest.execution_id != execution.id
                or manifest.id != authority.workspace_ref
                or manifest.fencing_token != authority.fencing_token
                or current.confirmed_by_type != "user"
                or current.confirmed_by_id != authority.principal_id
            )
        ):
            raise GatewayMessageError(
                "original native source pins changed",
                code="native_handoff_source_changed",
            )
        prepared = NativeContextPreparationReceipt.model_validate(link.preparation)
        if (
            prepared.execution_id,
            prepared.authority_id,
            prepared.binding_version,
            prepared.package_id,
            prepared.package_hash,
        ) != (
            execution.id,
            authority.id,
            payload.source_binding_version,
            payload.source_context_package_id,
            payload.source_context_package_hash,
        ):
            raise GatewayMessageError(
                "original native context changed", code="native_handoff_context_changed"
            )
        return execution, session, authority, manifest

    def _begin(self, uow, current, source, operation_id, operation_type):
        execution, session, authority, _ = source
        operation = uow.gateway_operations.get_by_operation_id(
            operation_id, current.workspace_id, for_update=True
        )
        if operation is not None:
            if (
                operation.execution_id,
                operation.session_id,
                operation.operation_type,
                operation.lease_id,
                operation.fencing_token,
            ) != (
                execution.id,
                session.id,
                operation_type,
                authority.lease_id,
                authority.fencing_token,
            ):
                raise GatewayMessageError(
                    "native intention identity changed",
                    code="native_handoff_operation_changed",
                )
            return operation, False
        operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=operation_id,
            workspace_id=current.workspace_id,
            task_id=execution.task_id,
            execution_id=execution.id,
            session_id=session.id,
            operation_type=operation_type,
            module_instance_id=None,
            config_version=None,
            lease_id=authority.lease_id,
            fencing_token=authority.fencing_token,
            now=self._clock(),
        )
        operation.start(now=self._clock())
        uow.gateway_operations.add(operation)
        return operation, True

    def _run(self, payload):
        require_gateway_owner(self._owner_check)
        self._reader.require(
            payload.source_authority_id, "run.read", payload.workspace_id
        )
        snapshot = self._services.execution.get_run(
            payload.workspace_id,
            payload.source_run_id,
            grant_id=payload.source_authority_id,
        )
        require_gateway_owner(self._owner_check)
        self._reader.require(
            payload.source_authority_id, "run.read", payload.workspace_id
        )
        if (snapshot.workspace_id, snapshot.run_id, snapshot.profile_ref) != (
            payload.workspace_id,
            payload.source_run_id,
            payload.source_profile_ref,
        ):
            raise GatewayMessageError(
                "native Run observation mismatch", code="native_handoff_run_mismatch"
            )
        return snapshot

    def checkpoint(self, transition):
        with self._uow_factory() as uow:
            current = self._current(
                uow,
                transition,
                HandoffGateOperation.COORDINATOR_CHECKPOINT_SOURCE,
                lock=True,
            )
            if current.source_checkpoint_facts is not None:
                return HandoffCheckpointEvidence.from_native_mapping(
                    current.source_checkpoint_facts
                )
            source = self._source(uow, current, lock=True)
            execution, session, authority, manifest = source
            if (
                execution.status.value != "running"
                or session.status.value != "active"
                or manifest.lifecycle.value != "active"
            ):
                raise GatewayMessageError(
                    "native source is not active",
                    code="native_handoff_source_not_active",
                )
            operation_id = handoff_step_operation_id(
                current.id,
                current.phase,
                logical_attempt=current.logical_attempt,
                action="checkpoint",
            )
            self._begin(
                uow,
                current,
                source,
                operation_id,
                GatewayOperationType.HANDOFF_CHECKPOINT,
            )
        snapshot = self._run(current.native_payload)
        if snapshot.state.value not in {
            "running",
            "idle",
        } or snapshot.last_event_sequence < max(
            session.last_sequence, current.source_input_cutoff_sequence
        ):
            raise GatewayMessageError(
                "native source snapshot is stale",
                code="native_handoff_source_not_active",
            )
        payload = current.native_payload
        evidence = HandoffCheckpointEvidence(
            operation_id=operation_id,
            transition_id=current.id,
            source_session_id=session.id,
            accepted_sequence=snapshot.last_event_sequence,
            workspace_digest=workspace_manifest_digest(manifest),
            context_package_id=payload.source_context_package_id,
            context_package_hash=payload.source_context_package_hash,
            context_version=payload.source_worker_context_version,
            progress_summary_ref=f"execution:{execution.id}",
            artifact_refs=(),
            evidence_refs=(),
            quiescent=False,
            produced_at=self._clock(),
            schema_version=2,
            workspace_id=current.workspace_id,
            source_execution_id=execution.id,
            source_run_id=payload.source_run_id,
            source_authority_id=authority.id,
            source_binding_version=payload.source_binding_version,
            source_profile_ref=payload.source_profile_ref,
            observation_kind="cp_run_snapshot",
            expected_stop_operation_id=handoff_step_operation_id(
                current.id,
                HandoffPhase.STOP_SOURCE,
                logical_attempt=current.logical_attempt,
                action="stop_source",
            ),
        )
        with self._uow_factory() as uow:
            locked = self._current(
                uow,
                transition,
                HandoffGateOperation.COORDINATOR_CHECKPOINT_SOURCE,
                lock=True,
            )
            _, stored_session, _, stored_manifest = self._source(uow, locked, lock=True)
            if (
                stored_session.version != session.version
                or workspace_manifest_digest(stored_manifest)
                != evidence.workspace_digest
            ):
                raise GatewayMessageError(
                    "native source changed during observation",
                    code="native_handoff_source_changed",
                )
            previous = locked.version
            locked.record_checkpoint(
                sequence=evidence.accepted_sequence, facts=evidence.to_mapping()
            )
            uow.handoff_transitions.save(locked, previous)
            operation = uow.gateway_operations.get_by_operation_id(
                operation_id, locked.workspace_id, for_update=True
            )
            previous = operation.version
            operation.succeed(
                result_ref=f"checkpoint:{evidence.accepted_sequence}:{evidence.workspace_digest}",
                now=self._clock(),
            )
            uow.gateway_operations.save(operation, previous)
        return evidence

    def stop(self, transition):
        require_gateway_owner(self._owner_check)
        with self._uow_factory() as uow:
            current = self._current(
                uow, transition, HandoffGateOperation.COORDINATOR_STOP_SOURCE, lock=True
            )
            source = self._source(uow, current, lock=True)
            checkpoint = HandoffCheckpointEvidence.from_native_mapping(
                current.source_checkpoint_facts
            )
            operation_id = checkpoint.expected_stop_operation_id
            if operation_id is None:
                raise GatewayMessageError(
                    "native stop intention missing",
                    code="native_handoff_source_stop_invalid",
                )
            operation, first = self._begin(
                uow, current, source, operation_id, GatewayOperationType.STOP_SESSION
            )
            if operation.status is GatewayOperationStatus.FAILED:
                raise GatewayMessageError(
                    "original native stop failed",
                    code="native_handoff_source_stop_failed",
                )
            evidence = (
                None
                if current.source_stop_facts is None
                else HandoffStopEvidence.from_native_mapping(current.source_stop_facts)
            )
            if evidence is not None:
                evidence.assert_matches(checkpoint)
        payload = current.native_payload
        if evidence is None:
            try:
                self._reader.require(
                    payload.source_authority_id,
                    "run.stop" if first else "run.read",
                    payload.workspace_id,
                )
                require_gateway_owner(self._owner_check)
                receipt = (
                    self._services.execution.stop(
                        RunStop(
                            workspace_id=payload.workspace_id,
                            run_id=payload.source_run_id,
                            operation_id=operation_id,
                            grant_id=payload.source_authority_id,
                        )
                    )
                    if first
                    else self._services.execution.get_stop(
                        payload.workspace_id,
                        payload.source_run_id,
                        operation_id,
                        grant_id=payload.source_authority_id,
                    )
                )
                require_gateway_owner(self._owner_check)
                self._reader.require(
                    payload.source_authority_id, "run.read", payload.workspace_id
                )
                receipt = StopReceipt.model_validate(receipt.model_dump())
                if (
                    receipt.operation_id != operation_id
                    or receipt.run_id != payload.source_run_id
                ):
                    return None
                if receipt.status.value == "failed":
                    self._failed_stop(transition, operation_id)
                    raise GatewayMessageError(
                        "original native stop failed",
                        code="native_handoff_source_stop_failed",
                    )
                if (
                    receipt.status.value != "stopped"
                    or receipt.evidence_sequence is None
                ):
                    return None
                snapshot = self._run(payload)
                evidence = HandoffStopEvidence(
                    operation_id=operation_id,
                    source_session_id=current.source_session_id,
                    accepted_sequence=receipt.evidence_sequence,
                    stopped=True,
                    produced_at=self._clock(),
                    schema_version=2,
                    transition_id=current.id,
                    workspace_id=payload.workspace_id,
                    source_execution_id=payload.source_execution_id,
                    source_run_id=payload.source_run_id,
                    source_authority_id=payload.source_authority_id,
                    source_binding_version=payload.source_binding_version,
                    source_profile_ref=payload.source_profile_ref,
                    run_state=snapshot.state.value,
                    input_open=snapshot.input_open,
                    active_response_id=snapshot.active_response_id,
                    observed_sequence=snapshot.last_event_sequence,
                )
                evidence.assert_matches(checkpoint)
            except AgentError as exc:
                if exc.code == "gateway_owner_lost":
                    raise
                return None
            except (TimeoutError, ValidationError, ContractValidationError):
                return None
        require_gateway_owner(self._owner_check)
        with self._uow_factory() as uow:
            locked = self._current(
                uow, transition, HandoffGateOperation.COORDINATOR_STOP_SOURCE, lock=True
            )
            execution, session, authority, _ = self._source(uow, locked, lock=True)
            lease = uow.leases.get(
                authority.lease_id, locked.workspace_id, for_update=True
            )
            binding = uow.bindings.get_for_execution(
                execution.id, locked.workspace_id, for_update=True
            )
            worker = uow.workers.get(
                session.worker_id, locked.workspace_id, for_update=True
            )
            agent_binding = uow.agent_bindings.get(
                locked.workspace_id,
                payload.source_binding_target_type,
                payload.source_binding_target_id,
                for_update=True,
            )
            operation = uow.gateway_operations.get_by_operation_id(
                operation_id, locked.workspace_id, for_update=True
            )
            if operation.status is GatewayOperationStatus.SUCCEEDED:
                if (
                    execution.status.value == "canceled"
                    and session.status.value == "closed"
                    and lease is not None
                    and lease.status.value == "released"
                    and (
                        execution.last_fencing_token == authority.fencing_token
                        and lease.fencing_token == authority.fencing_token
                        and lease.worker_id == session.worker_id
                        and binding is not None
                        and binding.lease_id == lease.id
                        and binding.fencing_token == authority.fencing_token
                    )
                ):
                    return evidence
                raise GatewayMessageError(
                    "confirmed stop cleanup ownership changed",
                    code="native_handoff_source_owner_changed",
                )
            owned = (
                lease is not None
                and binding is not None
                and worker is not None
                and (
                    execution.active_lease_id == lease.id
                    and execution.last_fencing_token == authority.fencing_token
                    and lease.status.value == "active"
                    and lease.fencing_token == authority.fencing_token
                    and lease.execution_id == execution.id
                    and lease.expires_at > self._clock()
                    and binding.lease_id == lease.id
                    and binding.fencing_token == authority.fencing_token
                    and binding.worker_id == session.worker_id
                    and lease.worker_id == session.worker_id
                    and execution.assigned_worker_id == worker.id
                    and worker.current_load > 0
                    and agent_binding is not None
                    and agent_binding.version == payload.source_binding_version
                    and agent_binding.profile_ref == payload.source_profile_ref
                    and execution.status.value == "running"
                    and session.status.value == "active"
                    and operation.status is GatewayOperationStatus.RUNNING
                )
            )
            previous_transition = locked.version
            if locked.source_stop_facts is None:
                locked.record_stop(
                    sequence=evidence.accepted_sequence, facts=evidence.to_mapping()
                )
                uow.handoff_transitions.save(locked, previous_transition)
            if owned:
                require_gateway_owner(self._owner_check)
                assert lease is not None and worker is not None
                previous = session.version
                session.close(lost=False, now=self._clock())
                uow.sessions.save(session, previous)
                previous = execution.version
                execution.cancel(now=self._clock())
                uow.executions.save(execution, previous)
                previous = lease.version
                lease.release(
                    worker_id=worker.id,
                    fencing_token=authority.fencing_token,
                    now=self._clock(),
                )
                uow.leases.save(lease, previous)
                previous = worker.version
                worker.release()
                uow.workers.save(worker, previous)
                previous = operation.version
                operation.succeed(
                    result_ref=f"sequence:{evidence.accepted_sequence}",
                    now=self._clock(),
                )
                uow.gateway_operations.save(operation, previous)
                self._stop_audit(
                    uow,
                    locked,
                    execution,
                    session,
                    lease,
                    operation_id,
                    evidence,
                    previous_transition,
                )
        if not owned:
            raise GatewayMessageError(
                "confirmed stop cleanup ownership changed",
                code="native_handoff_source_owner_changed",
            )
        return evidence

    def _failed_stop(self, transition, operation_id):
        with self._uow_factory() as uow:
            current = self._current(
                uow, transition, HandoffGateOperation.COORDINATOR_STOP_SOURCE, lock=True
            )
            self._source(uow, current, lock=True)
            operation = uow.gateway_operations.get_by_operation_id(
                operation_id, current.workspace_id, for_update=True
            )
            if operation.status is GatewayOperationStatus.RUNNING:
                previous = operation.version
                operation.fail(
                    failure_code="native_handoff_source_stop_failed", now=self._clock()
                )
                uow.gateway_operations.save(operation, previous)

    @staticmethod
    def _stop_audit(
        uow,
        transition,
        execution,
        session,
        lease,
        operation_id,
        evidence,
        previous_transition,
    ):
        command = CommandEnvelope(
            id=uuid5(
                NAMESPACE_URL, f"sidera.gateway.handoff-stop-command:{operation_id}"
            ),
            workspace_id=transition.workspace_id,
            command_type="StopHandoffSource",
            target_type="handoff_transition",
            actor_type="system",
            actor_id="gateway",
            target_id=transition.id,
            expected_version=previous_transition,
            payload={
                "source_execution_id": str(execution.id),
                "source_session_id": str(session.id),
                "source_lease_id": str(lease.id),
                "transition_id": str(transition.id),
                "operation_id": str(operation_id),
                "accepted_sequence": evidence.accepted_sequence,
            },
            idempotency_key=f"gateway-handoff-stop:{operation_id}",
            correlation_id=transition.id,
        )
        events = tuple(
            _gateway_event(
                command.id,
                workspace_id=transition.workspace_id,
                aggregate_type=aggregate_type,
                aggregate_id=entity.id,
                aggregate_version=entity.version,
                event_type=event_type,
                payload={
                    "status": entity.status.value,
                    "transition_id": str(transition.id),
                },
            )
            for aggregate_type, entity, event_type in (
                ("execution", execution, "ExecutionCanceledByHandoff"),
                ("execution_session", session, "ExecutionSessionClosedByHandoff"),
                ("lease", lease, "LeaseReleasedByHandoff"),
            )
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=transition.id,
                aggregate_version=transition.version,
                event_ids=tuple(event.id for event in events),
            ),
        )
        for event in events:
            uow.events.append_with_outbox(event)


def native_proposal_projection(proposal):
    payload = proposal.payload
    return {
        "id": str(proposal.id),
        "proposal_schema_version": 2,
        "workspace_id": str(payload.workspace_id),
        "root_task_id": str(payload.root_task_id),
        "subtask_id": str(payload.subtask_id),
        "worker_item_id": str(payload.worker_item_id),
        "source_worker_item_version": payload.source_worker_item_version,
        "version": proposal.version,
        "proposal_hash": proposal.proposal_hash,
        "status": proposal.status.value,
        "stale_reason": proposal.stale_reason,
        "created_at": proposal.created_at.isoformat(),
        "expires_at": proposal.expires_at.isoformat(),
        "confirmed_at": None
        if proposal.confirmed_at is None
        else proposal.confirmed_at.isoformat(),
        "cancelled_at": None
        if proposal.cancelled_at is None
        else proposal.cancelled_at.isoformat(),
        "source_execution_id": str(payload.source_execution_id),
        "source_session_id": str(payload.source_session_id),
        "source_run_id": str(payload.source_run_id),
        "source_binding_version": payload.source_binding_version,
        "source_profile_ref": payload.source_profile_ref.model_dump(mode="json"),
        "target_assignee": {
            "kind": payload.target_assignee.kind,
            "id": str(payload.target_assignee.id),
        },
        "target_profile_ref": None
        if payload.target_profile_ref is None
        else payload.target_profile_ref.model_dump(mode="json"),
        "target_worker_id": None,
        "handoff_reason": payload.handoff_reason,
        "handoff_instruction": payload.handoff_instruction,
        "context": native_context_projection(payload),
        "plan_base": native_plan_projection(payload),
    }


def native_context_projection(payload, transition=None):
    return {
        "source_package_id": str(payload.source_context_package_id),
        "source_package_hash": payload.source_context_package_hash,
        "source_context_version": payload.source_worker_context_version,
        "target_package_id": None
        if transition is None or transition.target_context_package_id is None
        else str(transition.target_context_package_id),
        "target_package_hash": None
        if transition is None
        else transition.target_context_package_hash,
        "target_context_version": payload.impact_summary["worker_item_context_version"]
        if transition is None
        else transition.target_context_version,
    }


def native_plan_projection(payload):
    return {
        "snapshot_id": str(payload.observed_plan_snapshot_id),
        "generation": payload.observed_plan_generation,
        "head_version": payload.observed_plan_head_version,
    }


def native_transition_projection(transition, *, target_run_id=None):
    payload = transition.native_payload
    return {
        "id": str(transition.id),
        "handoff_schema_version": 2,
        "workspace_id": str(transition.workspace_id),
        "worker_item_id": str(transition.worker_item_id),
        "proposal_id": str(transition.proposal_id),
        "source_execution_id": str(transition.source_execution_id),
        "source_session_id": str(transition.source_session_id),
        "source_run_id": str(payload.source_run_id),
        "source_binding_version": payload.source_binding_version,
        "source_profile_ref": payload.source_profile_ref.model_dump(mode="json"),
        "target_assignee": {
            "kind": payload.target_assignee.kind,
            "id": str(payload.target_assignee.id),
        },
        "target_profile_ref": None
        if payload.target_profile_ref is None
        else payload.target_profile_ref.model_dump(mode="json"),
        "target_binding_version": transition.target_binding_version,
        "target_worker_id": None
        if transition.target_worker_id is None
        else str(transition.target_worker_id),
        "target_execution_id": None
        if transition.target_execution_id is None
        else str(transition.target_execution_id),
        "target_session_id": None
        if transition.target_session_id is None
        else str(transition.target_session_id),
        "target_run_id": None if target_run_id is None else str(target_run_id),
        "version": transition.version,
        "status": transition.status.value,
        "phase": transition.phase.value,
        "writer_state": transition.writer_state.value,
        "logical_attempt": transition.logical_attempt,
        "source_input_cutoff_sequence": transition.source_input_cutoff_sequence,
        "target_context_version": transition.target_context_version,
        "failure_code": transition.failure_code,
        "blocked_operation_id": None
        if transition.blocked_operation_id is None
        else str(transition.blocked_operation_id),
        "next_attempt_at": None
        if transition.next_attempt_at is None
        else transition.next_attempt_at.isoformat(),
        "confirmed_at": transition.confirmed_at.isoformat(),
        "started_at": None
        if transition.started_at is None
        else transition.started_at.isoformat(),
        "completed_at": None
        if transition.completed_at is None
        else transition.completed_at.isoformat(),
        "context": native_context_projection(payload, transition),
        "plan_base": native_plan_projection(payload),
        "available_recovery_strategies": [],
    }
