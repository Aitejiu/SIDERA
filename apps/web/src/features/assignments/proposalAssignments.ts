import type { LocalSession } from "@/features/local-session/api";
import type { PlanningProposal } from "@/features/planning/types";
import { assignmentTargetKey } from "./api";
import {
  AssignmentUnavailableError,
  type AgentBindingView,
  type AssigneeValue,
  type PlanningAssigneeChoice,
  type ProfileView,
  type ProposalAssignmentTarget,
  type RevisionDiff,
} from "./types";

type TargetKind = ProposalAssignmentTarget["kind"];
type Strategy = ProposalAssignmentTarget["strategy"];

export function extractInitialAssignmentTargets(
  proposal: PlanningProposal,
): ProposalAssignmentTarget[] {
  const display = record(proposal.display);
  const rawSubtasks = array(display.subtasks);
  const rawWorkerItems = array(display.worker_items);
  const subtaskKeys = new Set<string>();
  const workerKeys = new Set<string>();
  const targets: ProposalAssignmentTarget[] = [];

  for (const rawSubtask of rawSubtasks) {
    const item = record(rawSubtask);
    const key = exactKey(item.key);
    if (subtaskKeys.has(key)) unavailable();
    subtaskKeys.add(key);
    targets.push({
      kind: "subtask",
      key,
      label: displayLabel(item.title, `Subtask ${key}`),
      subtaskKey: null,
      strategy: "create",
      existingId: null,
    });
  }

  for (const rawWorkerItem of rawWorkerItems) {
    const item = record(rawWorkerItem);
    const key = exactKey(item.key);
    const subtaskKey = exactKey(item.subtask_key);
    if (workerKeys.has(key) || !subtaskKeys.has(subtaskKey)) unavailable();
    workerKeys.add(key);
    targets.push({
      kind: "worker_item",
      key,
      label: displayLabel(item.title, `Worker item ${key}`),
      subtaskKey,
      strategy: "create",
      existingId: null,
    });
  }

  return targets;
}

export function extractRevisionAssignmentTargets(diff: RevisionDiff): ProposalAssignmentTarget[] {
  const subtaskChanges = array(diff.subtask_changes);
  const workerItemChanges = array(diff.worker_item_changes);
  const subtaskKeys = new Set<string>();
  const workerKeys = new Set<string>();
  const targets: ProposalAssignmentTarget[] = [];

  for (const rawChange of subtaskChanges) {
    const change = record(rawChange);
    const key = exactKey(change.target_subtask_key);
    const strategy = exactStrategy(change.strategy);
    const existingId = exactExistingId(change.existing_task_id, strategy);
    if (subtaskKeys.has(key)) unavailable();
    subtaskKeys.add(key);
    targets.push({
      kind: "subtask",
      key,
      label: displayLabel(change.path, `Subtask ${key}`),
      subtaskKey: null,
      strategy,
      existingId,
    });
  }

  for (const rawChange of workerItemChanges) {
    const change = record(rawChange);
    const key = exactKey(change.target_worker_item_key);
    const subtaskKey = exactKey(change.to_subtask_key);
    const strategy = exactStrategy(change.strategy);
    const existingId = exactExistingId(change.existing_worker_item_id, strategy);
    if (workerKeys.has(key) || !subtaskKeys.has(subtaskKey)) unavailable();
    workerKeys.add(key);
    targets.push({
      kind: "worker_item",
      key,
      label: displayLabel(change.path, `Worker item ${key}`),
      subtaskKey,
      strategy,
      existingId,
    });
  }

  return targets;
}

export function toPlanningAssigneeChoices({
  targets,
  choices,
  session,
  permittedProfiles,
  reusedBindings,
}: {
  targets: readonly ProposalAssignmentTarget[];
  choices: ReadonlyMap<string, AssigneeValue>;
  session: LocalSession;
  permittedProfiles: readonly ProfileView[];
  reusedBindings: ReadonlyMap<string, AgentBindingView>;
}): PlanningAssigneeChoice[] {
  validateSession(session);
  const checkedTargets = validateTargets(targets);
  const targetChoiceKeys = new Set(checkedTargets.map((target) =>
    assignmentTargetKey(target.kind, target.key),
  ));
  for (const choiceKey of choices.keys()) {
    if (!targetChoiceKeys.has(choiceKey)) unavailable();
  }

  return [...checkedTargets]
    .sort(compareTargets)
    .flatMap((target) => {
      const draftKey = assignmentTargetKey(target.kind, target.key);
      const draftChoice = choices.get(draftKey);
      if (target.strategy === "reuse") {
        if (target.existingId === null) unavailable();
        const bindingKey = assignmentTargetKey(target.kind, target.existingId);
        const binding = reusedBindings.get(bindingKey);
        validateBinding(binding, session.workspace_id, target);
        if (draftChoice === undefined) return [];
        if (!sameAssignee(draftChoice, binding)) unavailable();
        return [choiceFor(target, binding.assignee.kind, binding.assignee.id,
          binding.profile_ref?.revision ?? null, binding.version)];
      }

      if (draftChoice === undefined) unavailable();
      const exactChoice = validateNewChoice(draftChoice, session, permittedProfiles);
      return [choiceFor(
        target,
        exactChoice.kind,
        exactChoice.id,
        exactChoice.profileRef?.revision ?? null,
        null,
      )];
    });
}

function validateTargets(targets: readonly ProposalAssignmentTarget[]): ProposalAssignmentTarget[] {
  if (!Array.isArray(targets)) unavailable();
  const subtaskKeys = new Set<string>();
  const workerKeys = new Set<string>();
  const checked: ProposalAssignmentTarget[] = [];

  for (const target of targets) {
    if (!isRecord(target) || (target.kind !== "subtask" && target.kind !== "worker_item")) unavailable();
    const key = exactKey(target.key);
    const strategy = exactStrategy(target.strategy);
    if (target.kind === "subtask") {
      const existingId = exactExistingId(target.existingId, strategy);
      if (target.subtaskKey !== null || subtaskKeys.has(key)) unavailable();
      subtaskKeys.add(key);
      checked.push({
        kind: target.kind,
        key,
        label: displayLabel(target.label, `Subtask ${key}`),
        subtaskKey: null,
        strategy,
        existingId,
      });
      continue;
    }

    const subtaskKey = exactKey(target.subtaskKey);
    const existingId = exactExistingId(target.existingId, strategy);
    if (workerKeys.has(key)) unavailable();
    workerKeys.add(key);
    checked.push({
      kind: target.kind,
      key,
      label: displayLabel(target.label, `Worker item ${key}`),
      subtaskKey,
      strategy,
      existingId,
    });
  }

  for (const target of checked) {
    if (target.kind === "worker_item" && !subtaskKeys.has(target.subtaskKey!)) unavailable();
  }
  return checked;
}

function validateSession(session: LocalSession): void {
  const expiry = Date.parse(session.expires_at);
  if (!session.workspace_id.trim() || !session.session_id.trim() || !session.principal_id.trim()
    || !Number.isFinite(expiry) || expiry <= Date.now()) unavailable();
}

function validateBinding(
  binding: AgentBindingView | undefined,
  workspaceId: string,
  target: ProposalAssignmentTarget,
): asserts binding is AgentBindingView {
  if (binding === undefined || binding.workspace_id !== workspaceId
    || binding.target_type !== target.kind || binding.target_id !== target.existingId
    || !Number.isInteger(binding.version) || binding.version < 1
    || !isRecord(binding.assignee) || !nonblank(binding.assignee.id)) unavailable();
  if (binding.assignee.kind === "human") {
    if (binding.profile_ref !== null) unavailable();
    return;
  }
  if (binding.assignee.kind !== "profile" || binding.profile_ref === null
    || binding.profile_ref.workspace_id !== workspaceId
    || binding.profile_ref.profile_id !== binding.assignee.id
    || !Number.isInteger(binding.profile_ref.revision) || binding.profile_ref.revision < 1) {
    unavailable();
  }
}

function validateNewChoice(
  value: AssigneeValue,
  session: LocalSession,
  permittedProfiles: readonly ProfileView[],
): AssigneeValue {
  if (!isRecord(value) || !nonblank(value.id)) unavailable();
  if (value.kind === "human") {
    if (value.id !== session.principal_id || value.profileRef !== null) unavailable();
    return value;
  }
  if (value.kind !== "profile" || !isRecord(value.profileRef)
    || value.profileRef.workspace_id !== session.workspace_id
    || value.profileRef.profile_id !== value.id
    || !Number.isInteger(value.profileRef.revision) || value.profileRef.revision < 1) unavailable();

  const allowed = permittedProfiles.some((profile) =>
    profile.status.enabled
    && profile.profile.ref.workspace_id === value.profileRef.workspace_id
    && profile.profile.ref.profile_id === value.profileRef.profile_id
    && profile.profile.ref.revision === value.profileRef.revision,
  );
  if (!allowed) unavailable();
  return value;
}

function sameAssignee(choice: AssigneeValue, binding: AgentBindingView): boolean {
  if (choice.kind !== binding.assignee.kind || choice.id !== binding.assignee.id) return false;
  if (choice.kind === "human") return choice.profileRef === null && binding.profile_ref === null;
  return binding.profile_ref !== null
    && choice.profileRef.workspace_id === binding.profile_ref.workspace_id
    && choice.profileRef.profile_id === binding.profile_ref.profile_id
    && choice.profileRef.revision === binding.profile_ref.revision;
}

function choiceFor(
  target: ProposalAssignmentTarget,
  assigneeKind: "human" | "profile",
  assigneeId: string,
  profileRevision: number | null,
  expectedBindingVersion: number | null,
): PlanningAssigneeChoice {
  return {
    target_type: target.kind,
    target_key: target.key,
    assignee_kind: assigneeKind,
    assignee_id: assigneeId,
    profile_revision: profileRevision,
    expected_binding_version: expectedBindingVersion,
  };
}

function exactExistingId(value: unknown, strategy: Strategy): string | null {
  if (strategy === "reuse") {
    if (!nonblank(value) || value !== value.trim()) unavailable();
    return value;
  }
  if (value !== null) unavailable();
  return null;
}

function exactStrategy(value: unknown): Strategy {
  if (value === "create" || value === "derive" || value === "reuse") return value;
  unavailable();
}

function exactKey(value: unknown): string {
  if (!nonblank(value) || value !== value.trim()) unavailable();
  return value;
}

function displayLabel(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim().length > 0 ? value : fallback;
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) unavailable();
  return value;
}

function record(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) unavailable();
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function compareTargets(left: ProposalAssignmentTarget, right: ProposalAssignmentTarget): number {
  const rank: Record<TargetKind, number> = { subtask: 0, worker_item: 1 };
  if (rank[left.kind] !== rank[right.kind]) return rank[left.kind]! - rank[right.kind]!;
  return left.key < right.key ? -1 : left.key > right.key ? 1 : 0;
}

function unavailable(): never {
  throw new AssignmentUnavailableError();
}
