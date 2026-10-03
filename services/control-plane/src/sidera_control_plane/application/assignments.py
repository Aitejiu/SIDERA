from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sidera_agents.api import AgentCatalog, AgentRuntimes
from sidera_agents.contracts import ProfileRef
from sidera_agents.errors import AgentError
from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.agent_bindings import (
    AgentBinding,
    AssigneeRef,
    AssignmentDenied,
    HandoffRequired,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.events import EventData

ASSIGNMENT_COMMAND_TYPES = frozenset(
    {"SetSubtaskAssignee", "SetWorkerItemAssignee", "UpdateUnstartedAgentBinding"}
)


@dataclass(frozen=True, slots=True)
class TrustedAssignmentIdentity:
    principal_id: str
    grant_id: UUID
    actor_type: str = "user"


@dataclass(frozen=True, slots=True)
class AssignmentTarget:
    workspace_id: UUID
    target_type: str
    target_id: UUID
    principal_id: str


def require_trusted_assignment_identity(envelope, identity):
    if identity is None or not identity.principal_id:
        raise AssignmentDenied("trusted assignment identity required")
    if (
        envelope.actor_id != identity.principal_id
        or envelope.actor_type != identity.actor_type
    ):
        raise AssignmentDenied("assignment actor does not match trusted principal")


class AssignmentCommandHandler:
    command_types = ASSIGNMENT_COMMAND_TYPES

    def __init__(
        self,
        *,
        catalog: AgentCatalog | None = None,
        runtimes: AgentRuntimes | None = None,
        identity: TrustedAssignmentIdentity | None = None,
        authorizer: ExecutionAuthorizer | None = None,
    ):
        self._catalog, self._runtimes = catalog, runtimes
        self._identity, self._authorizer = identity, authorizer

    def _authorize(self, workspace_id, target_type, target_id, action):
        if self._identity is None or self._authorizer is None:
            raise AssignmentDenied("trusted assignment scope required")
        if target_type not in {"subtask", "worker_item", "execution"} or not isinstance(
            target_id, UUID
        ):
            raise ValidationError("valid assignment target required")
        try:
            self._authorizer.require(
                self._identity.grant_id,
                action,
                AssignmentTarget(
                    workspace_id, target_type, target_id, self._identity.principal_id
                ),
            )
        except AgentError:
            raise AssignmentDenied("assignment scope denied") from None

    def preflight(self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope):
        if envelope.command_type not in self.command_types:
            return
        require_trusted_assignment_identity(envelope, self._identity)
        target_type = _target_type(envelope.target_type)
        self._authorize(
            envelope.workspace_id, target_type, envelope.target_id, "assignment.write"
        )
        for kind, target_id, _ in _targets(envelope):
            self._authorize(envelope.workspace_id, kind, target_id, "assignment.write")

    def handle(self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope):
        if envelope.command_type not in self.command_types:
            raise ValidationError("unsupported assignment command")
        self.preflight(uow, envelope)
        payload = envelope.payload
        allowed = {"assignee_kind", "assignee_id", "profile_revision"}
        if envelope.command_type == "UpdateUnstartedAgentBinding":
            allowed.add("targets")
        if set(payload) != allowed:
            raise ValidationError("invalid assignment payload")
        revision = payload["profile_revision"]
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValidationError("profile revision must be positive")
        if not isinstance(payload["assignee_id"], (UUID, str)):
            raise ValidationError("assignee id must be UUID or text")
        assignee = AssigneeRef(payload["assignee_kind"], payload["assignee_id"])
        if assignee.kind == "human" and (
            str(assignee.id) != self._identity.principal_id
            or payload.get("profile_revision") is not None
        ):
            raise AssignmentDenied("human must be the trusted current local user")
        targets = _targets(envelope)
        prepared = []
        requirements = frozenset()
        for kind, target_id, version in sorted(
            targets, key=lambda t: (t[0], str(t[1]))
        ):
            repository = uow.tasks if kind == "subtask" else uow.worker_items
            aggregate = repository.get(
                target_id, envelope.workspace_id, for_update=True
            )
            if aggregate is None:
                raise AssignmentDenied("assignment target unavailable")
            if aggregate.version != version:
                raise VersionConflict(version, aggregate.version)
            if kind == "subtask":
                aggregate._assert_mutable()
            else:
                aggregate._assert_not_retired()
            active, required = uow.agent_bindings.execution_facts(
                envelope.workspace_id, kind, target_id
            )
            if active:
                raise HandoffRequired("active or uncertain execution requires handoff")
            requirements |= required
            current = uow.agent_bindings.get(
                envelope.workspace_id, kind, target_id, for_update=True
            )
            prepared.append((kind, target_id, aggregate, repository, current))
        pin, reasons = None, ()
        if assignee.kind == "profile":
            pin, reasons = self.resolve_profile(
                envelope.workspace_id,
                assignee.id,
                payload.get("profile_revision"),
                requirements,
            )
        now = datetime.now(UTC)
        bindings, events, versions = [], [], {}
        for kind, target_id, aggregate, repository, current in prepared:
            binding = AgentBinding(
                envelope.workspace_id,
                kind,
                target_id,
                assignee,
                pin,
                1 if current is None else current.version + 1,
                now if current is None else current.created_at,
                now,
                runnable=pin is not None and not reasons,
                not_runnable_reasons=reasons,
            )
            uow.agent_bindings.append(binding)
            previous = aggregate.version
            aggregate.version += 1
            if kind == "worker_item":
                aggregate.updated_at = now
            repository.save(aggregate, previous)
            event_id = uuid4()
            uow.events.append_with_outbox(
                EventData(
                    id=event_id,
                    workspace_id=envelope.workspace_id,
                    aggregate_type="task" if kind == "subtask" else kind,
                    aggregate_id=target_id,
                    aggregate_version=aggregate.version,
                    event_type="AgentBindingChanged",
                    payload=binding.to_mapping(),
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    causation_id=envelope.id,
                    correlation_id=envelope.correlation_id,
                )
            )
            bindings.append(binding.to_mapping())
            events.append(event_id)
            versions[target_id] = aggregate.version
        primary = next(b for b in bindings if b["target_id"] == str(envelope.target_id))
        return CommandResult(
            envelope.id,
            envelope.target_id,
            versions[envelope.target_id],
            tuple(events),
            {"binding": primary, "bindings": bindings},
        )

    def resolve_profile(
        self, workspace_id, profile_id, revision, requirements=frozenset()
    ):
        if self._identity is None or self._catalog is None or self._runtimes is None:
            raise AssignmentDenied("trusted profile services unavailable")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValidationError("profile revision must be positive")
        grant = self._identity.grant_id
        try:
            if revision is None:
                candidates = [
                    p.ref
                    for p in self._catalog.list_profiles(workspace_id, grant_id=grant)
                    if p.ref.workspace_id == workspace_id
                    and p.ref.profile_id == profile_id
                ]
                if not candidates:
                    raise AssignmentDenied("profile unavailable")
                ref = max(candidates, key=lambda p: p.revision)
            else:
                ref = ProfileRef(
                    workspace_id=workspace_id, profile_id=profile_id, revision=revision
                )
            profile = self._catalog.get_profile(ref, grant_id=grant)
            status = self._catalog.get_profile_status(ref, grant_id=grant)
            if (
                profile.ref != ref
                or status.profile_id != profile_id
                or not status.enabled
            ):
                raise AssignmentDenied("profile disabled or unavailable")
            validation = self._runtimes.validate_profile(ref, grant_id=grant)
            if profile.draft.runtime_ref.workspace_id != workspace_id:
                raise AssignmentDenied("profile configuration unavailable")
            runtime = self._runtimes.get_runtime(
                profile.draft.runtime_ref, grant_id=grant
            )
            if runtime.ref != profile.draft.runtime_ref:
                raise AssignmentDenied("profile runtime mismatch")
        except AgentError:
            raise AssignmentDenied("profile unavailable") from None
        reasons = [
            issue.code
            for issue in validation.issues
            if issue.code != "authentication_unconfirmed"
        ]
        if not validation.valid and not reasons:
            reasons.append("profile_configuration_invalid")
        if not runtime.installed or not runtime.enabled:
            reasons.append("runtime_unavailable")
        if runtime.protocol_state.value != "ready":
            reasons.append("protocol_" + runtime.protocol_state.value)
        if runtime.auth_state.value == "failed":
            reasons.append("authentication_" + runtime.auth_state.value)
        for capability in sorted(requirements):
            support = runtime.capabilities.get(capability)
            if support is None:
                reasons.append("required_capability_unavailable:" + capability)
            elif support.status.value != "supported":
                reasons.append(support.reason_code)
        return ref, tuple(dict.fromkeys(reasons))

    def get_assignee(self, uow, workspace_id, target_type, target_id):
        self._authorize(workspace_id, target_type, target_id, "assignment.read")
        repository = uow.tasks if target_type == "subtask" else uow.worker_items
        if repository.get(target_id, workspace_id) is None:
            raise AssignmentDenied("assignment target unavailable")
        return uow.agent_bindings.get(workspace_id, target_type, target_id)

    def get_agent_execution_link(self, uow, workspace_id, execution_id):
        # Authorize the opaque execution target before looking up its mapping.
        self._authorize(workspace_id, "execution", execution_id, "assignment.read")
        execution = uow.executions.get(execution_id, workspace_id)
        if execution is None:
            raise AssignmentDenied("execution unavailable")
        kind, target_id = (
            ("worker_item", execution.worker_item_id)
            if execution.worker_item_id is not None
            else ("subtask", execution.task_id)
        )
        self._authorize(workspace_id, kind, target_id, "assignment.read")
        return uow.execution_authorities.get_link(workspace_id, execution_id)


def _target_type(value):
    if value == "task":
        return "subtask"
    if value == "worker_item":
        return value
    raise ValidationError("assignment envelope target must be task or worker_item")


def _version(value):
    if type(value) is not int or value < 1:
        raise ValidationError("exact positive expected aggregate version required")
    return value


def _targets(envelope):
    kind = _target_type(envelope.target_type)
    if not isinstance(envelope.target_id, UUID):
        raise ValidationError("assignment target id required")
    primary = (kind, envelope.target_id, _version(envelope.expected_version))
    if envelope.command_type == "SetSubtaskAssignee" and kind != "subtask":
        raise ValidationError("subtask assignment requires task envelope")
    if envelope.command_type == "SetWorkerItemAssignee" and kind != "worker_item":
        raise ValidationError("worker item assignment requires worker_item envelope")
    if envelope.command_type != "UpdateUnstartedAgentBinding":
        return (primary,)
    raw = envelope.payload.get("targets")
    if not isinstance(raw, list) or not raw:
        raise ValidationError("explicit unstarted targets required")
    targets = []
    for target in raw:
        if not isinstance(target, dict) or set(target) != {
            "target_type",
            "target_id",
            "expected_version",
        }:
            raise ValidationError("invalid unstarted target")
        if target["target_type"] not in {"subtask", "worker_item"}:
            raise ValidationError("unknown unstarted target type")
        try:
            target_id = UUID(str(target["target_id"]))
        except ValueError:
            raise ValidationError("unstarted target id must be UUID") from None
        targets.append(
            (target["target_type"], target_id, _version(target["expected_version"]))
        )
    if primary not in targets or len({(k, i) for k, i, _ in targets}) != len(targets):
        raise ValidationError("unstarted targets must include primary exactly once")
    return tuple(targets)
