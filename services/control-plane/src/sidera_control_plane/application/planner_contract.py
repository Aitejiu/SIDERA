from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
)
from sidera_control_plane.application.planner_runtime import (
    PlannerClarificationResult,
    PlannerInput,
    PlannerProposalResult,
    PlannerResult,
)
from sidera_control_plane.application.revision_planning_context import (
    RevisionPlanningContextV1,
)
from sidera_control_plane.domain.collaboration import (
    resolve_collaboration_policy,
    resolve_collaboration_policy_v2,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.planning import (
    PlanningClarification,
    PlanningRequestKind,
    PlanningUsage,
)
from sidera_control_plane.domain.planning_profiles import PlanningProfileSnapshot
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    PlanningProposalPayloadV4,
    PlanningProposalPayloadV5,
    PlanningProposalPayloadV6,
    ProposalRevision,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
    PlanningRevisionProposalPayloadV5,
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.proposal_validation import validate_proposal
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer


class PlannerContractError(RuntimeError):
    """The strict CP planning output contract was not met."""


_MAX_KNOWLEDGE_CANDIDATES = 16
_MODEL_REF_SEQUENCE_KEYS = frozenset(
    {
        "acceptance_refs",
        "adapter_preferences",
        "artifact_refs",
        "capability_requirements",
        "context_refs",
        "evidence_refs",
        "expected_artifact_refs",
        "required_artifact_refs",
        "required_evidence_refs",
        "source_refs",
    }
)
_DEPENDENCY_PROMPT_CONTRACT = (
    "Each subtask_dependencies item must use exactly "
    "upstream_subtask_key, downstream_subtask_key, kind, "
    "required_evidence_refs, and enforcement; never include "
    "required_artifact_refs in a subtask_dependencies item. "
    "Each worker_item_dependencies item must use exactly "
    "upstream_worker_item_key, downstream_worker_item_key, kind, and "
    "required_artifact_refs; never include required_evidence_refs in a "
    "worker_item_dependencies item. Subtask dependencies carry required "
    "acceptance evidence; WorkerItem dependencies carry required artifacts. "
)


def _proposal_shape_hint(
    schema_version: int = 2,
    *,
    collaboration_strategy_id: str | None = None,
) -> dict[str, object]:
    shape: dict[str, object] = {
        "result_type": "proposal",
        "proposal": {
            "schema_version": 2,
            "kind": "create",
            "root_task": {
                "title": "one root task",
                "goal_summary": "the requested outcome",
                "acceptance_refs": ["acceptance:one"],
            },
            "subtasks": [
                {
                    "key": "subtask-one",
                    "kind": "child",
                    "title": "one subtask",
                    "goal_summary": "a concrete implementation step",
                    "acceptance_refs": ["acceptance:one"],
                    "expected_outputs": ["diff"],
                    "milestone_key": None,
                    "source_refs": [],
                }
            ],
            "worker_items": [
                {
                    "key": "worker-one",
                    "subtask_key": "subtask-one",
                    "title": "one WorkerItem",
                    "role": "implementation",
                    "responsibility": "perform the implementation step",
                    "capability_requirements": ["code"],
                    "worker_policy_ref": None,
                    "configuration_ref": None,
                    "concurrency_limit": 1,
                    "source_refs": [],
                    "pinned_worker_id": None,
                }
            ],
            "milestones": [],
            "subtask_dependencies": [],
            "worker_item_dependencies": [],
            "execution_intents": [
                {
                    "worker_item_key": "worker-one",
                    "purpose": "perform the implementation step",
                    "context_refs": [],
                    "expected_artifact_refs": ["artifact_type:diff"],
                    "required_evidence_refs": ["acceptance:one"],
                    "capability_requirements": ["code"],
                    "adapter_preferences": ["opencode"],
                    "budget_class": "standard",
                    "deadline_seconds": None,
                    "not_before_seconds": None,
                    "max_attempts": 1,
                    "activation_dependencies": [],
                }
            ],
            "decision_summary": {
                "understanding": "the requested outcome",
                "assumptions": [],
                "risks": [],
                "estimated_cost_microunits": 0,
                "estimated_tokens": 0,
                "estimated_runtime_seconds": 60,
                "estimated_concurrency": 1,
                "scope_includes": ["the requested outcome"],
                "scope_excludes": [],
                "metadata": {},
            },
            "impact_policy": {
                "activate_immediately": True,
                "budget_level": "standard",
                "failure_policy": "retry_then_block",
                "manual_gate": False,
                "revision_operation": None,
            },
            "base_descriptor": None,
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microunits": 0,
            "runtime_seconds": 0,
            "context_bytes": 0,
        },
        "knowledge_candidates": [
            {
                "schema_version": 1,
                "operation": "create_page",
                "target_page_id": None,
                "expected_revision_id": None,
                "expected_revision_hash": None,
                "title": "candidate title",
                "slug": "candidate-slug",
                "summary": "candidate summary",
                "tags": ["project"],
                "sections": [
                    {
                        "section_key": "body",
                        "heading": "Body",
                        "summary": "section summary",
                        "body_markdown": "candidate reference content",
                        "ordinal": 0,
                    }
                ],
                "source_suggestions": [],
                "artifact_refs": [],
                "evidence_refs": [],
                "relations": [],
                "supersedes_candidate_id": None,
            }
        ],
    }
    if schema_version == 4:
        proposal = cast(dict[str, object], shape["proposal"])
        proposal.pop("base_descriptor", None)
        proposal["schema_version"] = 4
        proposal["collaboration_policy_request"] = {
            "mode": "disabled",
            "default_policy": {
                "strategy_id": collaboration_strategy_id or "all_required"
            },
            "subtask_overrides": [],
        }
    if schema_version == 5:
        proposal = cast(dict[str, object], shape["proposal"])
        proposal.pop("base_descriptor", None)
        proposal["schema_version"] = 5
        proposal["collaboration_policy_request"] = {
            "schema_version": 2,
            "mode": "disabled",
            "default_policy": {
                "strategy_id": collaboration_strategy_id or "all_required"
            },
            "subtask_overrides": [],
            "input_groups": [],
        }
    if schema_version == 6:
        proposal = cast(dict[str, object], shape["proposal"])
        proposal.pop("base_descriptor", None)
        proposal["schema_version"] = 6
        proposal["worker_items"] = [
            *cast(list[object], proposal["worker_items"]),
            {
                "key": "source-worker",
                "subtask_key": "subtask-one",
                "title": "one source WorkerItem",
                "role": "source",
                "responsibility": "produce an upstream implementation result",
                "capability_requirements": ["code"],
                "worker_policy_ref": None,
                "configuration_ref": None,
                "concurrency_limit": 1,
                "source_refs": [],
                "pinned_worker_id": None,
            },
        ]
        proposal["execution_intents"] = [
            *cast(list[object], proposal["execution_intents"]),
            {
                "worker_item_key": "source-worker",
                "purpose": "produce an upstream implementation result",
                "context_refs": [],
                "expected_artifact_refs": ["artifact_type:diff"],
                "required_evidence_refs": ["acceptance:one"],
                "capability_requirements": ["code"],
                "adapter_preferences": ["opencode"],
                "budget_class": "standard",
                "deadline_seconds": None,
                "not_before_seconds": None,
                "max_attempts": 1,
                "activation_dependencies": [],
            },
        ]
        proposal["collaboration_policy_request"] = {
            "schema_version": 2,
            "mode": "enabled",
            "default_policy": {
                "strategy_id": collaboration_strategy_id or "all_required"
            },
            "subtask_overrides": [],
            "input_groups": [
                {
                    "group_key": "implementation-inputs",
                    "title": "Implementation results",
                    "consumer_worker_item_key": "worker-one",
                    "candidates": [{"worker_item_key": "source-worker", "priority": 1}],
                    "requirements": {
                        "execution_status": "succeeded",
                        "required_artifact_refs": ["artifact_type:diff"],
                        "required_evidence_refs": ["acceptance:one"],
                        "evidence_status": "candidate",
                    },
                    "mode": "at_least",
                    "minimum_count": 1,
                    "selection_strategy": "plan_order",
                }
            ],
        }
        proposal["integration_spec"] = _integration_spec_shape(
            integration_worker_item_key="worker-one",
            allowed_worker_item_keys=["worker-one", "source-worker"],
        )
        cast(dict[str, object], proposal["integration_spec"])["input_group_keys"] = [
            "implementation-inputs"
        ]
    return shape


def _revision_proposal_shape_hint(
    schema_version: int = 3,
    *,
    collaboration_strategy_id: str | None = None,
) -> dict[str, object]:
    shape: dict[str, object] = {
        "result_type": "proposal",
        "proposal": {
            "schema_version": 3,
            "kind": "revision",
            "base_descriptor": {
                "schema_version": 1,
                "workspace_id": "workspace-uuid",
                "root_task_id": "root-task-uuid",
                "plan_snapshot_id": "base-snapshot-uuid",
                "plan_generation": 1,
                "plan_content_hash": "64-lowercase-hex-characters",
                "root_task_version": 1,
                "captured_at": "2026-01-01T00:00:00+00:00",
            },
            "root_task": {
                "title": "one root task",
                "goal_summary": "the revised requested outcome",
                "acceptance_refs": ["acceptance:one"],
            },
            "subtasks": [
                {
                    "key": "subtask-one",
                    "kind": "child",
                    "title": "one subtask",
                    "goal_summary": "a concrete revised implementation step",
                    "acceptance_refs": ["acceptance:one"],
                    "expected_outputs": ["diff"],
                    "milestone_key": None,
                    "source_refs": [],
                }
            ],
            "worker_items": [
                {
                    "key": "worker-one",
                    "subtask_key": "subtask-one",
                    "title": "one WorkerItem",
                    "role": "implementation",
                    "responsibility": "perform the revised implementation step",
                    "capability_requirements": ["code"],
                    "worker_policy_ref": None,
                    "configuration_ref": None,
                    "concurrency_limit": 1,
                    "source_refs": [],
                    "pinned_worker_id": None,
                }
            ],
            "milestones": [],
            "subtask_dependencies": [],
            "worker_item_dependencies": [],
            "execution_intents": [
                {
                    "worker_item_key": "worker-one",
                    "purpose": "perform the implementation step",
                    "context_refs": [],
                    "expected_artifact_refs": ["artifact_type:diff"],
                    "required_evidence_refs": ["acceptance:one"],
                    "capability_requirements": ["code"],
                    "adapter_preferences": ["opencode"],
                    "budget_class": "standard",
                    "deadline_seconds": None,
                    "not_before_seconds": None,
                    "max_attempts": 1,
                    "activation_dependencies": [],
                }
            ],
            "subtask_identity_decisions": [
                {
                    "target_subtask_key": "subtask-one",
                    "strategy": "reuse",
                    "existing_subtask_task_id": "subtask-task-uuid",
                    "derived_from_subtask_task_ids": [],
                    "reason": "the existing subtask remains applicable",
                }
            ],
            "worker_item_identity_decisions": [
                {
                    "target_worker_item_key": "worker-one",
                    "strategy": "reuse",
                    "existing_worker_item_id": "worker-item-uuid",
                    "derived_from_worker_item_ids": [],
                    "target_subtask_key": "subtask-one",
                    "context_disposition": "unchanged",
                    "change_reasons": [],
                }
            ],
            "retired_subtask_ids": [],
            "retired_worker_item_ids": [],
            "decision_summary": {
                "understanding": "the revised requested outcome",
                "assumptions": [],
                "risks": [],
                "estimated_cost_microunits": 0,
                "estimated_tokens": 0,
                "estimated_runtime_seconds": 60,
                "estimated_concurrency": 1,
                "scope_includes": ["the revised requested outcome"],
                "scope_excludes": [],
                "metadata": {},
            },
            "impact_policy": {
                "activate_immediately": True,
                "budget_level": "standard",
                "failure_policy": "block_affected_branch",
                "manual_gate": False,
                "revision_operation": "revise",
            },
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microunits": 0,
            "runtime_seconds": 0,
            "context_bytes": 0,
        },
    }
    if schema_version == 4:
        proposal = cast(dict[str, object], shape["proposal"])
        proposal["schema_version"] = 4
        proposal["collaboration_policy_request"] = {
            "mode": "disabled",
            "default_policy": {
                "strategy_id": collaboration_strategy_id or "all_required"
            },
            "subtask_overrides": [],
        }
    if schema_version == 5:
        proposal = cast(dict[str, object], shape["proposal"])
        proposal["schema_version"] = 5
        proposal["collaboration_policy_request"] = {
            "schema_version": 2,
            "mode": "disabled",
            "default_policy": {
                "strategy_id": collaboration_strategy_id or "all_required"
            },
            "subtask_overrides": [],
            "input_groups": [],
        }
    if schema_version == 6:
        proposal = cast(dict[str, object], shape["proposal"])
        proposal["schema_version"] = 6
        proposal["worker_items"] = [
            *cast(list[object], proposal["worker_items"]),
            {
                "key": "source-worker",
                "subtask_key": "subtask-one",
                "title": "one source WorkerItem",
                "role": "source",
                "responsibility": "produce an upstream implementation result",
                "capability_requirements": ["code"],
                "worker_policy_ref": None,
                "configuration_ref": None,
                "concurrency_limit": 1,
                "source_refs": [],
                "pinned_worker_id": None,
            },
        ]
        proposal["execution_intents"] = [
            *cast(list[object], proposal["execution_intents"]),
            {
                "worker_item_key": "source-worker",
                "purpose": "produce an upstream implementation result",
                "context_refs": [],
                "expected_artifact_refs": ["artifact_type:diff"],
                "required_evidence_refs": ["acceptance:one"],
                "capability_requirements": ["code"],
                "adapter_preferences": ["opencode"],
                "budget_class": "standard",
                "deadline_seconds": None,
                "not_before_seconds": None,
                "max_attempts": 1,
                "activation_dependencies": [],
            },
        ]
        proposal["collaboration_policy_request"] = {
            "schema_version": 2,
            "mode": "enabled",
            "default_policy": {
                "strategy_id": collaboration_strategy_id or "all_required"
            },
            "subtask_overrides": [],
            "input_groups": [
                {
                    "group_key": "implementation-inputs",
                    "title": "Implementation results",
                    "consumer_worker_item_key": "worker-one",
                    "candidates": [{"worker_item_key": "source-worker", "priority": 1}],
                    "requirements": {
                        "execution_status": "succeeded",
                        "required_artifact_refs": ["artifact_type:diff"],
                        "required_evidence_refs": ["acceptance:one"],
                        "evidence_status": "candidate",
                    },
                    "mode": "at_least",
                    "minimum_count": 1,
                    "selection_strategy": "plan_order",
                }
            ],
        }
        proposal["integration_spec"] = _integration_spec_shape(
            integration_worker_item_key="worker-one",
            allowed_worker_item_keys=["worker-one", "source-worker"],
        )
        cast(dict[str, object], proposal["integration_spec"])["input_group_keys"] = [
            "implementation-inputs"
        ]
    return shape


def _integration_spec_shape(
    *, integration_worker_item_key: str, allowed_worker_item_keys: list[str]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository_ref": "configured-repository-ref",
        "base_commit": "40-lowercase-hex-characters",
        "integration_worker_item_key": integration_worker_item_key,
        "input_group_keys": [],
        "criteria": [
            {
                "key": "implementation",
                "text": "The implementation is ready",
                "mode": "automatic",
                "check_keys": ["unit"],
            }
        ],
        "checks": [
            {
                "key": "unit",
                "version": 1,
                "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
                "cwd": ".",
                "environment_ref": "configured-environment-ref",
                "prepare_argvs": [],
                "timeout_seconds": 60,
                "success_exit_codes": [0],
                "report_paths": [],
                "required": True,
            }
        ],
        "verification_binding": {
            "adapter_module_ref": "verification-adapter-module-uuid",
            "adapter_config_version": 1,
            "workspace_module_ref": "workspace-provider-module-uuid",
            "workspace_config_version": 1,
        },
        "rework_policy": {
            "max_auto_rework_rounds": 2,
            "max_auto_rework_seconds": 1800,
            "allowed_worker_item_keys": allowed_worker_item_keys,
        },
        "apply_policy": {
            "apply_on_accept": False,
            "mode": "fast_forward_only",
            "target_ref": None,
        },
        "finalization_policy": "stop_unused",
    }


def native_planning_instructions(planner_input: PlannerInput) -> tuple[str, ...]:
    """Business schema guidance; final context assembly belongs to agents."""
    shape = (
        _revision_proposal_shape_hint(planner_input.proposal_schema_version)
        if planner_input.kind is PlanningRequestKind.REVISION
        else _proposal_shape_hint(planner_input.proposal_schema_version)
    )
    shape.pop("usage", None)
    capabilities = [item.name for item in planner_input.capability_catalog.capabilities]

    def apply_catalog(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "capability_requirements":
                    value[key] = capabilities[:1]
                elif key == "adapter_preferences":
                    value[key] = list(
                        planner_input.policy_constraints.adapter_preferences
                    )
                else:
                    apply_catalog(item)
        elif isinstance(value, list):
            for item in value:
                apply_catalog(item)

    apply_catalog(shape)
    metadata = {
        "planning_profile_id": str(planner_input.profile_snapshot.profile_id),
        "planning_profile_version": planner_input.profile_snapshot.profile_version,
        "planner_agent_ref": planner_input.profile_snapshot.planner_agent_ref.model_dump(
            mode="json"
        ),
        "capabilities": [
            {"name": item.name, "evidence": item.description}
            for item in planner_input.capability_catalog.capabilities
        ],
        "allowed_capabilities": list(
            planner_input.policy_constraints.allowed_capabilities
        ),
        "max_nodes": planner_input.policy_constraints.max_nodes,
        "max_tokens": planner_input.policy_constraints.max_tokens,
        "max_cost_microunits": planner_input.policy_constraints.max_cost_microunits,
        "max_runtime_seconds": planner_input.policy_constraints.max_runtime_seconds,
        "max_concurrency": planner_input.policy_constraints.max_concurrency,
    }
    return (
        "Return one strict JSON object matching the selected CP proposal schema, or a clarification result. "
        "No prose, markdown fences, extra keys or model-authored usage accounting. "
        "Clarifications use result_type=clarification and clarifications containing question, rationale, answer_type, options and blocking. "
        "Use only named catalog capabilities with truthful evidence; an unconfirmed feature is not verified support. "
        "Responsibilities are independent of runtime features. Profile choices require later explicit human confirmation; pinned_worker_id is not a Profile ID. "
        + _DEPENDENCY_PROMPT_CONTRACT,
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        json.dumps(shape, sort_keys=True, separators=(",", ":")),
    )


def parse_planner_result(
    text: str,
    planner_input: PlannerInput,
    provider_ref: str,
    *,
    observed_usage: PlanningUsage | None = None,
) -> PlannerResult:
    try:
        value = json.loads(text)
        if not isinstance(value, Mapping):
            raise TypeError("response must be an object")
        result_type = value.get("result_type")
        if observed_usage is not None:
            if not isinstance(observed_usage, PlanningUsage):
                raise TypeError("observed usage must be typed")
            if "usage" in value:
                _parse_usage(value["usage"])
            usage = observed_usage
        else:
            usage = _parse_usage(value.get("usage"))
        if result_type == "proposal":
            _strict_keys(
                value,
                {"result_type", "proposal", "usage", "knowledge_candidates"},
            )
            raw_payload = value.get("proposal")
            if not isinstance(raw_payload, Mapping):
                raise ValueError("proposal must be an object")
            # Providers occasionally collapse a one-item JSON array into a
            # scalar. Normalize only fields whose domain schema explicitly
            # requires a sequence; all other shape validation stays strict.
            raw_payload = cast(
                Mapping[str, object], _normalize_model_ref_sequences(raw_payload)
            )
            if planner_input.proposal_schema_version == 6:
                _validate_v6_result_integration_shape(
                    raw_payload,
                    planner_input.profile_snapshot,
                )
                raw_payload = _normalize_result_integration_spec(
                    raw_payload,
                    planner_input.profile_snapshot,
                )
            derived_diff = None
            payload: (
                PlanningProposalPayload
                | PlanningProposalPayloadV4
                | PlanningProposalPayloadV5
                | PlanningProposalPayloadV6
                | PlanningRevisionProposalPayloadV3
                | PlanningRevisionProposalPayloadV4
                | PlanningRevisionProposalPayloadV5
                | PlanningRevisionProposalPayloadV6
            )
            if planner_input.kind is PlanningRequestKind.REVISION:
                if not isinstance(
                    planner_input.planning_context, RevisionPlanningContextV1
                ):
                    raise ValueError("revision planner context is missing")
                payload = (
                    PlanningRevisionProposalPayloadV6.from_mapping(raw_payload)
                    if planner_input.proposal_schema_version == 6
                    else PlanningRevisionProposalPayloadV5.from_mapping(raw_payload)
                    if planner_input.proposal_schema_version == 5
                    else PlanningRevisionProposalPayloadV4.from_mapping(raw_payload)
                    if planner_input.proposal_schema_version == 4
                    else PlanningRevisionProposalPayloadV3.from_mapping(raw_payload)
                )
                base_plan = TaskPlanSnapshot.from_payload(
                    dict(planner_input.planning_context.plan_snapshot_payload)
                )
                validate_proposal(
                    payload,
                    profile_snapshot=planner_input.profile_snapshot,
                    capability_snapshot=planner_input.profile_snapshot.planner_capabilities,
                    brief_revision=planner_input.brief_revision,
                    kind=PlanningRequestKind.REVISION,
                    base_snapshot=base_plan,
                )
                collaboration_policy = (
                    resolve_collaboration_policy_v2(
                        payload.collaboration_policy_request,
                        planner_input.profile_snapshot.collaboration_runtime_binding,
                        subtask_keys=frozenset(item.key for item in payload.subtasks),
                    )
                    if isinstance(
                        payload,
                        (
                            PlanningRevisionProposalPayloadV5,
                            PlanningRevisionProposalPayloadV6,
                        ),
                    )
                    else resolve_collaboration_policy(
                        payload.collaboration_policy_request,
                        planner_input.profile_snapshot.collaboration_runtime_binding,
                        subtask_keys=frozenset(item.key for item in payload.subtasks),
                    )
                    if isinstance(payload, PlanningRevisionProposalPayloadV4)
                    else None
                )
                derived_diff = RevisionDiffNormalizer().normalize(
                    base_plan,
                    payload,
                    collaboration_policy=collaboration_policy,
                )
            else:
                payload = (
                    PlanningProposalPayloadV6.from_mapping(raw_payload)
                    if planner_input.proposal_schema_version == 6
                    else PlanningProposalPayloadV5.from_mapping(raw_payload)
                    if planner_input.proposal_schema_version == 5
                    else PlanningProposalPayloadV4.from_mapping(raw_payload)
                    if planner_input.proposal_schema_version == 4
                    else PlanningProposalPayload.from_mapping(raw_payload)
                )
                payload = validate_proposal(
                    payload,
                    profile_snapshot=planner_input.profile_snapshot,
                    capability_snapshot=planner_input.profile_snapshot.planner_capabilities,
                    brief_revision=planner_input.brief_revision,
                    kind=PlanningRequestKind.CREATE,
                )
            proposal = ProposalRevision.create(
                planning_request_id=planner_input.planning_request_id,
                revision=1,
                brief_revision=planner_input.brief_revision,
                planning_run_id=planner_input.planning_run_id,
                kind=payload.kind,
                payload=payload,
                profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                    planner_input.profile_snapshot
                ),
                profile_snapshot=planner_input.profile_snapshot,
                created_at=datetime.now(UTC),
                derived_diff=derived_diff,
            )
            raw_candidates = value.get("knowledge_candidates", [])
            candidates: list[KnowledgeCandidateProposalV1] = []
            diagnostics: list[str] = []
            if not isinstance(raw_candidates, list):
                diagnostics.append("knowledge_candidates_not_a_list")
                raw_candidates = []
            for index, item in enumerate(raw_candidates[:_MAX_KNOWLEDGE_CANDIDATES]):
                if not isinstance(item, Mapping):
                    diagnostics.append(f"knowledge_candidate_{index}_not_an_object")
                    continue
                try:
                    candidates.append(KnowledgeCandidateProposalV1.from_mapping(item))
                except (TypeError, ValueError):
                    diagnostics.append(f"knowledge_candidate_{index}_invalid")
            if len(raw_candidates) > _MAX_KNOWLEDGE_CANDIDATES:
                diagnostics.append("knowledge_candidates_limit_exceeded")
            return PlannerProposalResult(
                proposal=proposal,
                usage=usage,
                knowledge_candidates=tuple(candidates),
                knowledge_candidate_diagnostics=tuple(diagnostics),
            )
        if result_type == "clarification":
            raw_items = _clarification_items(value)
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("clarifications must be a non-empty list")
            clarifications = tuple(
                _clarification(item, planner_input, index)
                for index, item in enumerate(raw_items)
            )
            return PlannerClarificationResult(
                clarifications=clarifications,
                usage=usage,
            )
        raise ValueError("result_type must be proposal or clarification")
    except (DomainError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PlannerContractError("strict JSON planner response is invalid") from exc


def _strict_keys(value: Mapping[str, object], allowed: set[str]) -> None:
    unknown = [str(key) for key in value if key not in allowed]
    if unknown:
        raise ValueError("response contains unknown fields")


def _normalize_model_ref_sequences(value: object) -> object:
    if isinstance(value, Mapping):
        normalized = {
            str(key): (
                []
                if str(key) in _MODEL_REF_SEQUENCE_KEYS and item is None
                else [item]
                if str(key) in _MODEL_REF_SEQUENCE_KEYS and isinstance(item, str)
                else _normalize_model_ref_sequences(item)
            )
            for key, item in value.items()
        }
        # Providers sometimes omit optional reference arrays entirely. Add
        # defaults only for recognizable proposal record shapes; required
        # non-empty arrays remain subject to the domain model validation.
        if {
            "upstream_subtask_key",
            "downstream_subtask_key",
        }.issubset(normalized):
            normalized.setdefault("required_evidence_refs", [])
        if {
            "upstream_worker_item_key",
            "downstream_worker_item_key",
            "kind",
        }.issubset(normalized):
            normalized.setdefault("required_artifact_refs", [])
        if {"worker_item_key", "purpose"}.issubset(normalized):
            for key in (
                "context_refs",
                "expected_artifact_refs",
                "required_evidence_refs",
                "capability_requirements",
                "adapter_preferences",
                "activation_dependencies",
            ):
                normalized.setdefault(key, [])
        return normalized
    if isinstance(value, list):
        return [_normalize_model_ref_sequences(item) for item in value]
    return value


def _result_integration_environment(
    profile_snapshot: PlanningProfileSnapshot,
) -> dict[str, object] | None:
    raw_environment = profile_snapshot.system_policy.get("result_integration")
    if not isinstance(raw_environment, Mapping):
        return None

    environment: dict[str, object] = {}
    for key in ("repository_ref", "base_commit", "verification_environment_ref"):
        value = raw_environment.get(key)
        if isinstance(value, str) and value:
            environment[key] = value

    raw_check_specs = raw_environment.get("verification_check_specs")
    if isinstance(raw_check_specs, (list, tuple)):
        check_specs = [
            dict(item) for item in raw_check_specs if isinstance(item, Mapping)
        ]
        if check_specs:
            environment["verification_check_specs"] = check_specs

    raw_binding = raw_environment.get("verification_binding")
    if isinstance(raw_binding, Mapping):
        binding = {
            key: raw_binding[key]
            for key in (
                "adapter_module_ref",
                "adapter_config_version",
                "workspace_module_ref",
                "workspace_config_version",
            )
            if key in raw_binding
        }
        if len(binding) == 4:
            environment["verification_binding"] = binding

    return environment or None


def _normalize_result_integration_spec(
    value: Mapping[str, object],
    profile_snapshot: PlanningProfileSnapshot,
) -> Mapping[str, object]:
    environment = _result_integration_environment(profile_snapshot)
    raw_spec = value.get("integration_spec")
    if environment is None or not isinstance(raw_spec, Mapping):
        return value

    spec = dict(raw_spec)
    for key in ("repository_ref", "base_commit"):
        configured = environment.get(key)
        if isinstance(configured, str) and configured:
            spec[key] = configured
    configured_binding = environment.get("verification_binding")
    if isinstance(configured_binding, Mapping):
        spec["verification_binding"] = dict(configured_binding)
    configured_environment_ref = environment.get("verification_environment_ref")
    raw_checks = spec.get("checks")
    configured_check_specs = environment.get("verification_check_specs")
    configured_check_spec_list = (
        configured_check_specs if isinstance(configured_check_specs, list) else []
    )
    configured_checks_by_key = {
        str(item["key"]): item
        for item in configured_check_spec_list
        if isinstance(item, Mapping) and isinstance(item.get("key"), str)
    }
    if (
        isinstance(configured_environment_ref, str) or configured_checks_by_key
    ) and isinstance(raw_checks, (list, tuple)):
        normalized_checks: list[object] = []
        for check in raw_checks:
            if isinstance(check, Mapping):
                configured = configured_checks_by_key.get(str(check.get("key")))
                normalized_check = (
                    dict(configured) if configured is not None else dict(check)
                )
                if isinstance(configured_environment_ref, str):
                    normalized_check["environment_ref"] = configured_environment_ref
                normalized_checks.append(normalized_check)
            else:
                normalized_checks.append(check)
        spec["checks"] = normalized_checks

    normalized = dict(value)
    normalized["integration_spec"] = spec
    return normalized


def _validate_v6_result_integration_shape(
    value: Mapping[str, object],
    profile_snapshot: PlanningProfileSnapshot,
) -> None:
    if _result_integration_environment(profile_snapshot) is None:
        return
    policy = value.get("collaboration_policy_request")
    if not isinstance(policy, Mapping):
        return
    if policy.get("mode") != "enabled":
        raise ValueError(
            "schema v6 result integration requires collaboration_policy_request.mode=enabled"
        )
    groups = policy.get("input_groups")
    if not isinstance(groups, (list, tuple)) or not groups:
        raise ValueError(
            "schema v6 result integration requires a non-empty input_groups array"
        )
    spec = value.get("integration_spec")
    if not isinstance(spec, Mapping):
        return
    group_keys = spec.get("input_group_keys")
    if not isinstance(group_keys, (list, tuple)) or not group_keys:
        raise ValueError(
            "schema v6 integration_spec.input_group_keys must be non-empty"
        )
    declared_group_keys = {
        str(item.get("group_key"))
        for item in groups
        if isinstance(item, Mapping) and isinstance(item.get("group_key"), str)
    }
    missing = {str(key) for key in group_keys} - declared_group_keys
    if missing:
        raise ValueError(
            "schema v6 integration_spec.input_group_keys must match input_groups"
        )


def _clarification_items(value: Mapping[str, object]) -> object:
    if "clarifications" in value:
        _strict_keys(value, {"result_type", "clarifications", "usage"})
        return value.get("clarifications")

    # Some providers flatten a single clarification despite the envelope prompt.
    # Normalize only that exact shape; arbitrary extra fields remain invalid.
    fields = {"question", "rationale", "answer_type", "options", "blocking"}
    _strict_keys(value, {"result_type", "usage", *fields})
    return [{field: value.get(field) for field in fields}]


def _parse_usage(value: object) -> PlanningUsage:
    if not isinstance(value, Mapping):
        raise TypeError("usage must be an object")
    fields = {
        "input_tokens",
        "output_tokens",
        "cost_microunits",
        "runtime_seconds",
        "context_bytes",
    }
    _strict_keys(value, fields)
    parsed: dict[str, int] = {}
    for field in fields:
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool):
            raise TypeError("usage values must be integers")
        parsed[field] = item
    return PlanningUsage(**parsed)


def _clarification(
    value: object, planner_input: PlannerInput, index: int
) -> PlanningClarification:
    if not isinstance(value, Mapping):
        raise TypeError("clarification must be an object")
    allowed = {"question", "rationale", "answer_type", "options", "blocking"}
    _strict_keys(value, allowed)
    question = value.get("question")
    rationale = value.get("rationale")
    answer_type = value.get("answer_type")
    options = value.get("options")
    blocking = value.get("blocking")
    if not all(isinstance(item, str) for item in (question, rationale, answer_type)):
        raise ValueError("clarification text fields must be strings")
    if not isinstance(options, list) or not all(
        isinstance(item, str) for item in options
    ):
        raise ValueError("clarification options must be strings")
    if not isinstance(blocking, bool):
        raise TypeError("clarification blocking must be boolean")
    question_text = cast(str, question)
    rationale_text = cast(str, rationale)
    answer_type_text = cast(str, answer_type)
    option_values = cast(list[str], options)
    return PlanningClarification.create(
        clarification_id=uuid5(
            NAMESPACE_URL,
            f"sidera.opencode.planner:{planner_input.operation_id}:clarification:{index}",
        ),
        planning_request_id=planner_input.planning_request_id,
        planning_run_id=planner_input.planning_run_id,
        question=question_text,
        rationale=rationale_text,
        answer_type=answer_type_text,
        options=option_values,
        blocking=blocking,
        created_at=datetime.now(UTC),
    )
