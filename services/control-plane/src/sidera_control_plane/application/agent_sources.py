"""Exact, authority-scoped CP fact sources, not a prompt assembler."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sidera_agents.contracts import ContextItem, SourceContent, SourceRef
from sidera_agents.errors import AgentError

from sidera_control_plane.application.context_materialization import (
    MAX_CONTEXT_PAYLOAD_BYTES,
    ContextMaterializationError,
    ContextMaterializer,
    ValidatedContextFacts,
)
from sidera_control_plane.application.context_requests import (
    load_execution_context_selection,
    resolve_execution_context_authorization,
)
from sidera_control_plane.application.execution_authority import (
    PlanningActionTarget,
    require_active_planning,
)
from sidera_control_plane.application.revision_planning_context import (
    decode_native_revision_facts,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextMaterializationMode,
    ContextPackageManifestV2,
    ContextPackageManifestV3,
    ContextPurpose,
    ContextSelectionReason,
    ResultInputManifestV1,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_revision_proposals import _base_descriptor
from sidera_control_plane.domain.plans import TaskPlanSnapshot

_VERSION = re.compile(r"cp1:([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64})")
_KNOWLEDGE = re.compile(r"knowledge/(0|[1-9][0-9]*)")
_SECTIONS = {"task", "facts", "results", "code", "verification"}


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def canonical_source_text(value: object) -> str:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _deny():
    raise AgentError("permission_denied", "Task source access denied.")


@dataclass(frozen=True, slots=True)
class _SourceIdentity:
    package_id: UUID
    revision_id: UUID
    section: str
    package_hash: str
    revision_hash: str
    body_hash: str


def _identity(ref: SourceRef) -> _SourceIdentity:
    if not isinstance(ref, SourceRef) or ref.provider != "control-plane":
        _deny()
    parts = ref.key.split("/")
    version = _VERSION.fullmatch(ref.version)
    if len(parts) not in {4, 5} or parts[0] != "context" or version is None:
        _deny()
    try:
        package_id, revision_id = UUID(parts[1]), UUID(parts[2])
    except ValueError:
        _deny()
    if str(package_id) != parts[1] or str(revision_id) != parts[2]:
        _deny()
    section = "/".join(parts[3:])
    if section not in _SECTIONS and _KNOWLEDGE.fullmatch(section) is None:
        _deny()
    return _SourceIdentity(package_id, revision_id, section, *version.groups())


def _sections(facts: ValidatedContextFacts) -> tuple[tuple[str, bool], ...]:
    manifest = facts.manifest
    result = [("task", True), ("facts", True)]
    result.extend(
        (
            f"knowledge/{item.ordinal}",
            item.selection_reason is not ContextSelectionReason.RETRIEVAL,
        )
        for item in sorted(manifest.knowledge_refs, key=lambda item: item.ordinal)
    )
    if (
        isinstance(manifest, (ContextPackageManifestV2, ContextPackageManifestV3))
        and manifest.result_input_manifest is not None
    ):
        result.append(("results", True))
    if isinstance(manifest, ContextPackageManifestV3):
        if manifest.code_input_manifest is not None:
            result.append(("code", True))
        if manifest.verification_manifest is not None:
            result.append(("verification", True))
    return tuple(result)


def _source_text(facts: ValidatedContextFacts, section: str) -> str:
    if section == "task":
        revision = facts.revision
        value = {
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "snapshot": revision.snapshot.to_mapping(),
        }
    elif section == "facts":
        value = facts.manifest.control_plane_fact_snapshot
    elif section == "results":
        value = {
            "manifest": facts.manifest.result_input_manifest.to_mapping(),
            "inputs": [item.to_mapping() for item in facts.result_inputs],
        }
    elif section in {"code", "verification"}:
        value = getattr(
            facts.manifest,
            f"{section}_input_manifest"
            if section == "code"
            else "verification_manifest",
        ).to_mapping()
    else:
        ordinal = int(section.split("/")[1])
        value = next(
            item for item in facts.project_knowledge if item.ordinal == ordinal
        ).to_mapping()
    return canonical_source_text(value)


def context_source_items(facts: ValidatedContextFacts) -> tuple[ContextItem, ...]:
    """Seal validated immutable sections for an explicitly created CP authority."""
    result = []
    for ordinal, (section, required) in enumerate(_sections(facts)):
        digest = sha256(_source_text(facts, section).encode("utf-8")).hexdigest()
        ref = SourceRef(
            provider="control-plane",
            key=f"context/{facts.manifest.package_id}/{facts.revision.revision_id}/{section}",
            version=f"cp1:{facts.manifest.package_hash}:{facts.revision.content_hash}:{digest}",
        )
        result.append(_item(ref, section, ordinal, required))
    return tuple(result)


def _item(ref: SourceRef, section: str, ordinal: int, required: bool) -> ContextItem:
    return ContextItem(
        item_id=section,
        origin="control-plane",
        required=required,
        ordinal=ordinal,
        version=ref.version,
        content_hash="sha256:" + _identity(ref).body_hash,
        source_ref=ref,
    )


def _read_pinned_source(reader, grant_id: UUID, ref: SourceRef) -> SourceContent:
    facts = reader.validated_facts(grant_id, ref)
    identity = _identity(ref)
    try:
        if identity.section.startswith("knowledge/"):
            section = reader._materializer.read_knowledge_section(
                facts,
                int(identity.section.split("/")[1]),
                correlation_id=facts.manifest.consumer_id,
                caller_request_id=uuid4(),
            )
            facts = ValidatedContextFacts(
                facts.manifest, facts.revision, (section,), ()
            )
        elif identity.section == "results":
            facts = ValidatedContextFacts(
                facts.manifest,
                facts.revision,
                (),
                reader._materializer.read_result_inputs(facts.manifest),
            )
        text = _source_text(facts, identity.section)
    except ContextMaterializationError as exc:
        raise AgentError(exc.code, "Pinned task source is unavailable.") from None
    digest = sha256(text.encode("utf-8")).hexdigest()
    if digest != identity.body_hash:
        raise AgentError(
            "context_hash_mismatch", "Pinned task source integrity failed."
        )
    if len(text.encode("utf-8")) > MAX_CONTEXT_PAYLOAD_BYTES:
        raise AgentError(
            "context_budget_exceeded", "Pinned task source exceeds its budget."
        )
    reader._authorizer.require(grant_id, "source.read", ref)
    return SourceContent(
        text=text, actual_version=ref.version, content_hash="sha256:" + digest
    )


class TaskContextSourceReader:
    def __init__(
        self, uow_factory, *, authorizer, catalog, result_input_content=None, clock=None
    ):
        self._uow_factory, self._authorizer = uow_factory, authorizer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._materializer = ContextMaterializer(
            catalog, result_input_content=result_input_content
        )

    def require_execution(
        self, grant_id: UUID, workspace_id: UUID, execution_id: UUID, operation_id: UUID
    ) -> None:
        with self._uow_factory() as uow:
            authority = uow.execution_authorities.get(grant_id)
            if authority is None or not authority.sources:
                _deny()
            self._authorizer.require(grant_id, "source.read", authority.sources[0])
            link = uow.execution_authorities.get_link(
                authority.workspace_id, authority.execution_id
            )
            if (
                authority.workspace_id != workspace_id
                or authority.execution_id != execution_id
                or link is None
                or link.authority_id != grant_id
                or link.start_operation_id != operation_id
                or link.profile_ref != authority.profile_ref
                or link.binding_version != authority.binding_version
            ):
                _deny()

    def validated_facts(self, grant_id: UUID, ref: SourceRef) -> ValidatedContextFacts:
        # Authorization precedes package lookup and every external body read.
        self._authorizer.require(grant_id, "source.read", ref)
        identity = _identity(ref)
        try:
            with self._uow_factory() as uow:
                authority = uow.execution_authorities.get(grant_id)
                if authority is None:
                    _deny()
                execution = uow.executions.get(
                    authority.execution_id, authority.workspace_id
                )
                manifest = uow.context_packages.get(
                    identity.package_id, authority.workspace_id
                )
                revision = uow.work_contexts.get_revision(
                    identity.revision_id, authority.workspace_id
                )
                if execution is None or manifest is None or revision is None:
                    _deny()
                authorization = resolve_execution_context_authorization(uow, execution)
                if authorization is None:
                    _deny()
                request = authorization.request
                worker_item = uow.worker_items.get(
                    execution.worker_item_id, authority.workspace_id
                )
                context = uow.work_contexts.get_by_root_task(
                    request.root_task_id, authority.workspace_id
                )
                if (
                    manifest.consumer_type is not ContextConsumerType.WORKER
                    or manifest.consumer_id != execution.id
                    or manifest.worker_item_id != execution.worker_item_id
                    or worker_item is None
                    or request.subtask_id != execution.task_id
                    or worker_item.root_task_id != request.root_task_id
                    or manifest.context_version != worker_item.context_version
                    or context is None
                    or context.id != revision.work_context_id
                    or manifest.work_context_revision_id != revision.revision_id
                    or manifest.package_hash != identity.package_hash
                    or revision.content_hash != identity.revision_hash
                    or revision.snapshot.content_hash != revision.content_hash
                ):
                    _deny()
                snapshot = manifest.control_plane_fact_snapshot
                if (
                    snapshot.get("execution", {}).get("id") != str(execution.id)
                    or snapshot.get("execution_request", {}).get("id")
                    != str(request.id)
                    or snapshot.get("worker_item", {}).get("id")
                    != str(execution.worker_item_id)
                    or snapshot.get("plan", {}).get("id")
                    != str(request.plan_snapshot_id)
                    or snapshot.get("execution", {}).get("task_id")
                    != str(execution.task_id)
                    or tuple(
                        snapshot.get("execution_request", {}).get(
                            "expected_outputs", ()
                        )
                    )
                    != request.expected_outputs
                ):
                    _deny()
                selection = load_execution_context_selection(uow, request)
                pinned_results = getattr(manifest, "result_input_manifest", None)
                if selection is not None:
                    if (
                        pinned_results is None
                        or pinned_results
                        != ResultInputManifestV1.from_selection(
                            selection, budget=pinned_results.budget
                        )
                    ):
                        _deny()
                elif pinned_results is not None:
                    _deny()
                facts = self._materializer.validate_facts(
                    manifest,
                    revision,
                    mode=ContextMaterializationMode.CONSUMER_START,
                    workspace_id=authority.workspace_id,
                    consumer_id=execution.id,
                    purpose=ContextPurpose.EXECUTION,
                    context_version=manifest.context_version,
                    expected_package_hash=identity.package_hash,
                    now=self._clock(),
                    correlation_id=execution.id,
                    caller_request_id=uuid4(),
                    include_bodies=False,
                )
                if identity.section not in dict(_sections(facts)):
                    _deny()
                return facts
        except ContextMaterializationError as exc:
            raise AgentError(exc.code, "Pinned task context is unavailable.") from None
        except (DomainError, ValueError, TypeError, KeyError):
            raise AgentError(
                "context_invalid", "Pinned task context is invalid."
            ) from None

    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent:
        return _read_pinned_source(self, grant_id, ref)

    def context_items(
        self,
        grant_id: UUID,
        execution_id: UUID,
        *,
        reserved_bytes: int = 0,
        required_refs: tuple[SourceRef, ...] = (),
    ) -> tuple[ContextItem, ...]:
        if type(reserved_bytes) is not int or reserved_bytes < 0:
            raise ValueError("Reserved input bytes must be non-negative")
        with self._uow_factory() as uow:
            authority = uow.execution_authorities.get(grant_id)
            if (
                authority is None
                or authority.execution_id != execution_id
                or not authority.sources
            ):
                _deny()
            refs = authority.sources
        if len(set(required_refs)) != len(required_refs) or set(required_refs) - set(
            refs
        ):
            _deny()
        facts = self.validated_facts(grant_id, refs[0])
        identities = tuple(_identity(ref) for ref in refs)
        if len({identity.section for identity in identities}) != len(refs):
            _deny()
        if any(
            identity.package_id != facts.manifest.package_id
            or identity.revision_id != facts.revision.revision_id
            for identity in identities
        ):
            _deny()
        by_section = {
            identity.section: ref
            for identity, ref in zip(identities, refs, strict=True)
        }
        if set(by_section) - dict(_sections(facts)).keys():
            _deny()
        result, total_bytes = [], reserved_bytes
        for section, required in _sections(facts):
            ref = by_section.get(section)
            if ref is None:
                if required:
                    _deny()
                continue
            item = _item(ref, section, len(result), required)
            required = required or ref in required_refs
            if required and not item.required:
                item = item.model_copy(update={"required": True})
            try:
                content = self.read(grant_id, ref)
            except AgentError as exc:
                if required or exc.code not in {
                    "context_reference_not_found",
                    "context_service_unavailable",
                }:
                    raise
                # Keep the original optional pin; the agents assembler records omission.
            else:
                total_bytes += len(content.text.encode("utf-8"))
            result.append(item)
        if set(required_refs) - {item.source_ref for item in result}:
            _deny()
        limit = MAX_CONTEXT_PAYLOAD_BYTES
        if (
            isinstance(
                facts.manifest, (ContextPackageManifestV2, ContextPackageManifestV3)
            )
            and facts.manifest.result_input_manifest is not None
        ):
            limit = min(
                limit, facts.manifest.result_input_manifest.budget.max_payload_bytes
            )
        if (
            total_bytes > limit
            or (total_bytes + 3) // 4 > facts.manifest.budget.max_input_tokens
        ):
            raise AgentError(
                "context_budget_exceeded", "Pinned task context exceeds its budget."
            )
        return tuple(result)


class PlanningContextSourceReader:
    def __init__(self, uow_factory, *, authorizer, materializer, clock=None):
        self._uow_factory, self._authorizer, self._materializer = (
            uow_factory,
            authorizer,
            materializer,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def _facts(self, uow, run, request, manifest, revision, *, include_bodies):
        if request.kind is PlanningRequestKind.REVISION:
            base = request.base_descriptor
            if base is None or base.workspace_id != run.workspace_id:
                _deny()
            context = uow.work_contexts.get_by_root_task(
                base.root_task_id, run.workspace_id
            )
            if context is None or context.root_task_id != base.root_task_id:
                _deny()
        else:
            context = uow.work_contexts.get_by_planning_request(
                request.id, run.workspace_id
            )
        assembly = uow.context_assemblies.get_for_consumer(
            ContextPurpose.PLANNING_RUN, run.id, run.workspace_id
        )
        brief = next(
            (item for item in request.briefs if item.revision == run.brief_revision),
            None,
        )
        if (
            manifest is None
            or revision is None
            or brief is None
            or context is None
            or assembly is None
        ):
            _deny()
        snapshot = manifest.control_plane_fact_snapshot
        if (
            manifest.purpose is not ContextPurpose.PLANNING_RUN
            or manifest.consumer_type is not ContextConsumerType.PLANNER
            or manifest.consumer_id != run.id
            or manifest.worker_item_id is not None
            or manifest.context_version is not None
            or context.id != revision.work_context_id
            or context.current_revision_id != revision.revision_id
            or revision.brief_planning_request_id != request.id
            or revision.brief_revision != run.brief_revision
            or revision.snapshot.content_hash != revision.content_hash
            or assembly.status.value != "ready"
            or assembly.work_context_revision_id != revision.revision_id
            or assembly.budget.get("planner_agent_ref")
            != run.profile_snapshot.planner_agent_ref.model_dump(mode="json")
            or snapshot.get("planning_request", {}).get("id") != str(request.id)
            or snapshot.get("planning_run", {}).get("id") != str(run.id)
            or snapshot.get("planning_run", {}).get("operation_id")
            != str(run.operation_id)
            or snapshot.get("brief", {}).get("revision") != run.brief_revision
            or snapshot.get("brief", {}).get("planning_request_id") != str(request.id)
            or snapshot.get("brief", {}).get("objective") != brief.objective
            or tuple(snapshot.get("brief", {}).get("constraints", ()))
            != brief.constraints
            or tuple(snapshot.get("brief", {}).get("context_refs", ()))
            != brief.context_refs
            or tuple(snapshot.get("brief", {}).get("acceptance_criteria", ()))
            != brief.acceptance_criteria
            or tuple(snapshot.get("brief", {}).get("requested_outputs", ()))
            != brief.requested_outputs
            or snapshot.get("planning_profile", {}).get("profile_id")
            != str(run.profile_snapshot.profile_id)
            or snapshot.get("planning_profile", {}).get("profile_version")
            != run.profile_snapshot.profile_version
            or snapshot.get("planner_agent")
            != run.profile_snapshot.planner_agent_ref.model_dump(mode="json")
        ):
            _deny()
        if request.kind is PlanningRequestKind.REVISION:
            graph = snapshot.get("revision_base")
            budget_base = assembly.budget.get("revision_base")
            if not isinstance(graph, Mapping) or not isinstance(budget_base, Mapping):
                _deny()
            graph = decode_native_revision_facts(graph)
            declared_base = _base_descriptor(
                {
                    "schema_version": budget_base.get("schema_version"),
                    "workspace_id": budget_base.get("workspace_id"),
                    "root_task_id": budget_base.get("root_task_id"),
                    "plan_snapshot_id": budget_base.get("plan_id"),
                    "plan_generation": budget_base.get("generation"),
                    "plan_content_hash": budget_base.get("content_hash"),
                    "root_task_version": budget_base.get("root_task_version"),
                    "captured_at": budget_base.get("captured_at"),
                }
            )
            base = request.base_descriptor
            graph_base = _base_descriptor(graph.get("base_descriptor", {}))
            plan = TaskPlanSnapshot.from_payload(_plain(graph.get("plan_snapshot", {})))
            actual = (
                None
                if base is None
                else uow.plans.get(base.plan_snapshot_id, run.workspace_id)
            )
            if (
                base is None
                or declared_base != base
                or graph_base != base
                or actual is None
                or (
                    plan.id,
                    plan.workspace_id,
                    plan.root_task_id,
                    plan.generation,
                    plan_snapshot_content_hash(plan),
                    plan_snapshot_content_hash(actual),
                )
                != (
                    base.plan_snapshot_id,
                    base.workspace_id,
                    base.root_task_id,
                    base.plan_generation,
                    base.plan_content_hash,
                    base.plan_content_hash,
                )
            ):
                _deny()
        return self._materializer.validate_facts(
            manifest,
            revision,
            mode=ContextMaterializationMode.CONSUMER_START,
            workspace_id=run.workspace_id,
            consumer_id=run.id,
            purpose=ContextPurpose.PLANNING_RUN,
            context_version=None,
            expected_package_hash=manifest.package_hash,
            now=self._clock(),
            correlation_id=request.correlation_id,
            caller_request_id=uuid4(),
            include_bodies=include_bodies,
        )

    def prepare_facts(
        self,
        workspace_id,
        planning_run_id,
        *,
        principal_id,
        grant_id,
        upstream_authorizer,
        owner_id,
        lease_id,
        fencing_token,
    ):
        if upstream_authorizer is None or not principal_id:
            _deny()
        with self._uow_factory() as uow:
            run = uow.planning_runs.get(planning_run_id, workspace_id)
            if run is None:
                _deny()
            request = uow.planning_requests.get(run.planning_request_id, workspace_id)
            if request is None:
                _deny()
            lease = require_active_planning(
                uow,
                run,
                request,
                now=self._clock(),
                owner_id=owner_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
            )
            manifest = uow.context_packages.get_for_consumer(
                ContextPurpose.PLANNING_RUN, run.id, workspace_id
            )
            revision = (
                None
                if manifest is None
                else uow.work_contexts.get_revision(
                    manifest.work_context_revision_id, workspace_id
                )
            )
            facts = self._facts(
                uow, run, request, manifest, revision, include_bodies=False
            )
            saved_versions = (run.version, request.version)
            saved_lease = (lease.owner_id, lease.id, lease.fencing_token)
            assembly = uow.context_assemblies.get_for_consumer(
                ContextPurpose.PLANNING_RUN, run.id, workspace_id
            )
            saved_assembly = self._assembly_pin(assembly)
            target = PlanningActionTarget(
                workspace_id,
                principal_id,
                request.id,
                run.id,
                run.profile_snapshot.profile_id,
                run.profile_snapshot.profile_version,
                run.profile_snapshot.planner_agent_ref,
                run.operation_id,
                manifest,
            )
        upstream_authorizer.require(grant_id, "source.read", target)
        prepared = self._materializer.validate_facts(
            facts.manifest,
            facts.revision,
            mode=ContextMaterializationMode.CONSUMER_START,
            workspace_id=workspace_id,
            consumer_id=planning_run_id,
            purpose=ContextPurpose.PLANNING_RUN,
            context_version=None,
            expected_package_hash=facts.manifest.package_hash,
            now=self._clock(),
            correlation_id=request.correlation_id,
            caller_request_id=uuid4(),
            include_bodies=True,
        )
        with self._uow_factory() as uow:
            current_run = uow.planning_runs.get(planning_run_id, workspace_id)
            current_request = uow.planning_requests.get(
                target.planning_request_id, workspace_id
            )
            if (
                current_run is None
                or current_request is None
                or (current_run.version, current_request.version) != saved_versions
            ):
                _deny()
            require_active_planning(
                uow,
                current_run,
                current_request,
                now=self._clock(),
                owner_id=saved_lease[0],
                lease_id=saved_lease[1],
                fencing_token=saved_lease[2],
            )
            self.revalidate_prepared_facts(
                uow,
                current_run,
                current_request,
                facts,
                saved_assembly,
                principal_id=principal_id,
                grant_id=grant_id,
                upstream_authorizer=upstream_authorizer,
            )
        return prepared, saved_assembly

    def revalidate_prepared_facts(
        self,
        uow,
        run,
        request,
        facts,
        assembly_pin,
        *,
        principal_id,
        grant_id,
        upstream_authorizer,
    ):
        manifest = uow.context_packages.get_for_consumer(
            ContextPurpose.PLANNING_RUN, run.id, run.workspace_id
        )
        revision = uow.work_contexts.get_revision(
            facts.revision.revision_id, run.workspace_id
        )
        assembly = uow.context_assemblies.get_for_consumer(
            ContextPurpose.PLANNING_RUN, run.id, run.workspace_id
        )
        if (
            manifest != facts.manifest
            or revision != facts.revision
            or self._assembly_pin(assembly) != assembly_pin
        ):
            _deny()
        self._facts(uow, run, request, manifest, revision, include_bodies=False)
        upstream_authorizer.require(
            grant_id,
            "source.read",
            PlanningActionTarget(
                run.workspace_id,
                principal_id,
                request.id,
                run.id,
                run.profile_snapshot.profile_id,
                run.profile_snapshot.profile_version,
                run.profile_snapshot.planner_agent_ref,
                run.operation_id,
                facts.manifest,
            ),
        )

    @staticmethod
    def _assembly_pin(assembly):
        if assembly is None:
            _deny()
        return (
            assembly.request_id,
            assembly.request_fingerprint,
            assembly.work_context_revision_id,
            canonical_source_text(assembly.budget),
            assembly.status,
        )

    def validated_facts(self, grant_id: UUID, ref: SourceRef) -> ValidatedContextFacts:
        self._authorizer.require(grant_id, "source.read", ref)
        identity = _identity(ref)
        try:
            with self._uow_factory() as uow:
                authority = uow.planning_authorities.get(grant_id)
                if authority is None or ref not in authority.sources:
                    _deny()
                run = uow.planning_runs.get(
                    authority.planning_run_id, authority.workspace_id
                )
                request = uow.planning_requests.get(
                    authority.planning_request_id, authority.workspace_id
                )
                if run is None or request is None:
                    _deny()
                manifest = uow.context_packages.get(
                    identity.package_id, authority.workspace_id
                )
                revision = uow.work_contexts.get_revision(
                    identity.revision_id, authority.workspace_id
                )
                if (
                    manifest is None
                    or revision is None
                    or manifest.package_hash != identity.package_hash
                    or revision.content_hash != identity.revision_hash
                    or manifest.work_context_revision_id != revision.revision_id
                ):
                    _deny()
                facts = self._facts(
                    uow, run, request, manifest, revision, include_bodies=False
                )
                if identity.section not in dict(_sections(facts)):
                    _deny()
                return facts
        except ContextMaterializationError as exc:
            raise AgentError(
                exc.code, "Pinned planning context is unavailable."
            ) from None
        except (DomainError, ValueError, TypeError, KeyError):
            raise AgentError(
                "context_invalid", "Pinned planning context is invalid."
            ) from None

    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent:
        return _read_pinned_source(self, grant_id, ref)
