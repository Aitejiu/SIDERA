import { beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkContextDetail } from "@/features/context/types";
import type { CommandRequest } from "@/features/assignments/types";
import type { PlanningDraftScope } from "./planningConfirmationDraft";
import { planningDraftKey } from "./planningConfirmationDraft";
import { planningProposalDraftKey } from "./ProposalFeedbackComposer";
import type { PlanningRequest, PlanningRun } from "./types";
import {
  clearAppliedFailedPlanningFeedbackDraft, failedPlanningFeedbackDraftKey, failedPlanningFeedbackPayload,
  loadFailedPlanningFeedbackDraft, reviewFailedPlanningFeedback, saveFailedPlanningFeedbackDraft,
} from "./failedPlanningFeedback.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const HASH = "a".repeat(64);
const NOW = "2026-10-09T00:00:00Z";

function fixture(kind: "create" | "revision" = "create") {
  const scope: PlanningDraftScope = kind === "create"
    ? { kind, workspace_id: id(1), request_id: id(2), principal_id: "human-current" }
    : { kind, workspace_id: id(1), request_id: id(2), principal_id: "human-current", root_task_id: id(3) };
  const ref = { workspace_id: id(1), profile_id: id(4), revision: 2 };
  const run: PlanningRun = {
    id: id(5), workspace_id: id(1), planning_request_id: id(2), brief_revision: 2, attempt: 3,
    version: 7, status: "failed", result_kind: "failure", execution_mode: "native_profile", provider_ref: "agent-run",
    operation_id: id(6), planner_agent_ref: ref, capability_snapshot: null, error_code: "invalid_output",
    native_run: { status: "closed", closed: true, run_id: id(7), response_id: id(8), event_cursor: 9,
      start_operation_id: id(6), reason_code: null },
    usage: { input_tokens: null, output_tokens: null, runtime_seconds: null, context_bytes: null, cost_microunits: null },
    queued_at: NOW, started_at: NOW, finished_at: NOW,
  };
  const request: PlanningRequest = {
    id: id(2), workspace_id: id(1), kind, status: "failed", version: 12, current_brief_revision: 2,
    current_run_id: id(5), current_proposal_revision: null, current_proposal_content_hash: null,
    planning_profile_id: id(9), planning_profile_version: 3, profile_key: "native-planner",
    profile: { id: id(9), workspace_id: id(1), version: 3, profile_key: "native-planner", display_name: "Planner",
      execution_mode: "native_profile", status: "active", planner_agent_ref: ref, capabilities: null,
      planner_interface_version: "1.0", planner_module_instance_id: null, planner_module_config_version: null,
      planner_provider: null, user_selectable: true, created_at: NOW },
    active_run: structuredClone(run), pending_clarifications: [], usage: run.usage, requires_attention: true,
    source_type: "user", source_ref: null, last_change_cursor: 1, created_at: NOW, updated_at: NOW,
    revision_base: kind === "revision" ? { workspace_id: id(1), root_task_id: id(3), plan_snapshot_id: id(10),
      plan_content_hash: HASH, plan_generation: 1, root_task_version: 2, schema_version: 1, captured_at: NOW } : null,
  };
  const context: WorkContextDetail = {
    id: id(11), workspace_id: id(1), planning_request_id: kind === "create" ? id(2) : id(99),
    root_task_id: kind === "create" ? null : id(3), status: "active", version: 4,
    current_revision_id: id(12), current_revision_number: 3, created_at: NOW, updated_at: NOW, timeline: [], next_cursor: null,
    current_revision: { id: id(12), workspace_id: id(1), work_context_id: id(11), revision_number: 3,
      brief_planning_request_id: id(2), brief_revision: 2, content_hash: HASH, reason: "user_intervention",
      created_by: "human-current", created_at: NOW, plan_snapshot_id: null, proposal_revision_id: null,
      snapshot: { schema_version: 1, objective: "Retain original Task", constraints: [], acceptance_criteria: [],
        requested_outputs: [], accepted_decisions: [], clarification_decisions: [], unresolved_questions: [],
        artifact_refs: [], evidence_refs: [], checkpoint_refs: [], knowledge_refs: [], missing_fact_codes: [] } },
  };
  return { scope, request, runs: [run], context };
}

function basis(kind: "create" | "revision" = "create") {
  const result = reviewFailedPlanningFeedback(fixture(kind));
  if (!result.ok) throw new Error(result.reason);
  return result.basis;
}

beforeEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });

describe.each(["create", "revision"] as const)("failed planning feedback binding %s", (kind) => {
  it("pins the exact current closed Run and current Brief context", () => {
    expect(reviewFailedPlanningFeedback(fixture(kind))).toMatchObject({ ok: true, basis: {
      requestVersion: 12, failedRunId: id(5), failedRunVersion: 7, attempt: 3, briefRevision: 2,
      workContextId: id(11), workContextVersion: 4, workContextRevisionId: id(12),
      workContextRevisionNumber: 3, workContextContentHash: HASH,
    } });
    const input = fixture(kind);
    input.runs.push({ ...input.runs[0]!, id: id(33), version: 1, status: "succeeded" });
    expect(reviewFailedPlanningFeedback(input).ok).toBe(true);
  });

  it.each([
    ["request workspace", { workspace_id: id(99) }], ["request identity", { id: id(99) }],
    ["request kind", { kind: "foreign" }], ["request status", { status: "blocked" }],
    ["request version", { version: 0 }], ["boolean request version", { version: true }],
    ["Brief version", { current_brief_revision: 0 }], ["policy version", { planning_profile_version: 0 }],
    ["policy identity", { planning_profile_id: id(99) }], ["current Run", { current_run_id: id(99) }],
  ])("refuses a mismatched %s", (_name, changes) => {
    const input = fixture(kind);
    Object.assign(input.request, changes);
    expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
  });

  it.each([
    ["legacy policy", { execution_mode: "legacy" }], ["inactive policy", { status: "disabled" }],
    ["wrong policy version", { version: 4 }], ["wrong policy workspace", { workspace_id: id(99) }],
    ["missing agent", { planner_agent_ref: null }],
    ["foreign agent", { planner_agent_ref: { workspace_id: id(99), profile_id: id(4), revision: 2 } }],
    ["invalid agent revision", { planner_agent_ref: { workspace_id: id(1), profile_id: id(4), revision: 0 } }],
  ])("refuses %s", (_name, changes) => {
    const input = fixture(kind);
    Object.assign(input.request.profile!, changes);
    expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
  });

  it.each([
    ["workspace", { workspace_id: id(99) }], ["request", { planning_request_id: id(99) }],
    ["Brief", { brief_revision: 1 }], ["version", { version: 0 }], ["attempt", { attempt: 0 }],
    ["mode", { execution_mode: "legacy" }], ["status", { status: "succeeded" }],
    ["result", { result_kind: "proposal" }], ["provider suffix", { provider_ref: `agent-run:${id(7)}` }],
    ["missing provider", { provider_ref: null }], ["missing finish", { finished_at: null }],
    ["invalid finish", { finished_at: "invalid" }], ["agent", { planner_agent_ref: null }],
    ["operation", { operation_id: id(99) }],
  ])("refuses a Run with wrong %s", (_name, changes) => {
    const input = fixture(kind);
    Object.assign(input.runs[0]!, changes);
    input.request.active_run = structuredClone(input.runs[0]!);
    expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
  });

  it.each([
    ["status", { status: "unknown" }], ["closed", { closed: false }], ["Run ID", { run_id: null }],
    ["response ID", { response_id: "bad" }], ["cursor", { event_cursor: 0 }],
    ["boolean cursor", { event_cursor: true }], ["operation", { start_operation_id: id(99) }],
  ])("refuses unproven native closure %s", (_name, changes) => {
    const input = fixture(kind);
    Object.assign(input.runs[0]!.native_run!, changes);
    input.request.active_run = structuredClone(input.runs[0]!);
    expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
  });

  it("refuses missing, duplicate and foreign Run rows or contradictory active snapshots", () => {
    const input = fixture(kind);
    expect(reviewFailedPlanningFeedback({ ...input, runs: undefined }).ok).toBe(false);
    expect(reviewFailedPlanningFeedback({ ...input, runs: [] }).ok).toBe(false);
    expect(reviewFailedPlanningFeedback({ ...input, runs: [...input.runs, ...input.runs] }).ok).toBe(false);
    expect(reviewFailedPlanningFeedback({ ...input, runs: [...input.runs, { ...input.runs[0]!, id: id(33), workspace_id: id(99) }] }).ok).toBe(false);
    for (const changes of [{ version: 8 }, { brief_revision: 1 }, { operation_id: id(99) }, { native_run: null }, { planner_agent_ref: null }]) {
      input.request.active_run = { ...input.runs[0]!, ...changes };
      expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
    }
  });

  it.each([
    ["workspace", { workspace_id: id(99) }], ["status", { status: "inactive" }], ["version", { version: 0 }],
    ["revision ID", { current_revision_id: id(99) }], ["revision number", { current_revision_number: 4 }],
    ["owner", kind === "create" ? { planning_request_id: id(99) } : { root_task_id: id(99) }],
  ])("refuses context %s mismatch", (_name, changes) => {
    const input = fixture(kind);
    Object.assign(input.context, changes);
    expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
  });

  it.each([
    ["workspace", { workspace_id: id(99) }], ["context", { work_context_id: id(99) }],
    ["Brief request", { brief_planning_request_id: id(99) }], ["Brief revision", { brief_revision: 1 }],
    ["uppercase hash", { content_hash: "A".repeat(64) }], ["short hash", { content_hash: "a" }],
  ])("refuses context revision %s mismatch", (_name, changes) => {
    const input = fixture(kind);
    Object.assign(input.context.current_revision, changes);
    expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
  });

  it("emits exactly the six payload fields with trimmed explicit feedback", () => {
    expect(failedPlanningFeedbackPayload(basis(kind), "  Keep four workers  ")).toEqual({
      failed_planning_run_id: id(5), expected_failed_planning_run_version: 7,
      expected_work_context_version: 4, work_context_revision_id: id(12), work_context_content_hash: HASH,
      feedback: "Keep four workers",
    });
    expect(() => failedPlanningFeedbackPayload(basis(kind), " \n ")).toThrow();
  });

  it("round trips a closed text draft and refuses corrupt or foreign pins", () => {
    const pinned = basis(kind);
    expect(saveFailedPlanningFeedbackDraft(pinned, "Keep history")).toEqual({ ok: true });
    expect(loadFailedPlanningFeedbackDraft(pinned)).toEqual({ ok: true, feedback: "Keep history" });
    const key = failedPlanningFeedbackDraftKey(pinned);
    const raw = JSON.parse(window.localStorage.getItem(key)!);
    for (const damaged of ["{", JSON.stringify({ ...raw, private: true }), JSON.stringify({ ...raw, principal_id: "foreign" }),
      JSON.stringify({ ...raw, failed_planning_run_version: 8 }), JSON.stringify({ ...raw, schema_version: 2 })]) {
      window.localStorage.setItem(key, damaged);
      expect(loadFailedPlanningFeedbackDraft(pinned).ok).toBe(false);
    }
  });

  it("clears only the matching applied original's failed text key", () => {
    const pinned = basis(kind);
    saveFailedPlanningFeedbackDraft(pinned, "Keep history");
    const other = { ...pinned, failedRunVersion: 8 };
    saveFailedPlanningFeedbackDraft(other, "Other run");
    const proposalKey = planningProposalDraftKey(id(1), id(2), 2);
    const commandKey = planningDraftKey(pinned.scope);
    window.localStorage.setItem(proposalKey, "proposal-text");
    window.localStorage.setItem(commandKey, "command-original");
    const original: CommandRequest = { command_type: "ReviseFailedPlanningRequest", command_id: id(20),
      workspace_id: id(1), target_type: "planning_request", target_id: id(2), actor: { type: "user", id: "human-current" },
      expected_version: 12, correlation_id: id(21), idempotency_key: id(22), payload: failedPlanningFeedbackPayload(pinned, "Keep history") };
    const remaining = [failedPlanningFeedbackDraftKey(other), proposalKey, commandKey];
    const bytes = remaining.map((key) => window.localStorage.getItem(key));
    expect(clearAppliedFailedPlanningFeedbackDraft({ ...pinned.scope, principal_id: "foreign" }, original).ok).toBe(false);
    expect(window.localStorage.getItem(failedPlanningFeedbackDraftKey(pinned))).not.toBeNull();
    expect(clearAppliedFailedPlanningFeedbackDraft(pinned.scope, original)).toEqual({ ok: true });
    expect(window.localStorage.getItem(failedPlanningFeedbackDraftKey(pinned))).toBeNull();
    expect(remaining.map((key) => window.localStorage.getItem(key))).toEqual(bytes);
  });
});

it("separates users, kinds, requests, failed Runs and versions", () => {
  const value = basis();
  const values = [value, basis("revision"), { ...value, scope: { ...value.scope, principal_id: "human:other" } },
    { ...value, scope: { ...value.scope, workspace_id: id(88) } }, { ...value, scope: { ...value.scope, request_id: id(88) } },
    { ...value, failedRunId: id(88) }, { ...value, failedRunVersion: 8 }];
  expect(new Set(values.map(failedPlanningFeedbackDraftKey)).size).toBe(values.length);
  expect(failedPlanningFeedbackDraftKey(value)).toMatch(/^sidera:failed-planning-feedback:v1:/);
});

it("refuses the wrong revision base and request-owned fallback context", () => {
  const input = fixture("revision");
  input.request.revision_base = { ...input.request.revision_base!, root_task_id: id(99) };
  expect(reviewFailedPlanningFeedback(input).ok).toBe(false);
  const fallback = fixture("revision");
  fallback.context.root_task_id = null;
  fallback.context.planning_request_id = id(2);
  expect(reviewFailedPlanningFeedback(fallback).ok).toBe(false);
});

it("returns typed storage read, write and cleanup failures", () => {
  const pinned = basis();
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("unavailable"); });
  expect(loadFailedPlanningFeedbackDraft(pinned).ok).toBe(false);
  vi.restoreAllMocks();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
  expect(saveFailedPlanningFeedbackDraft(pinned, "keep").ok).toBe(false);
  vi.restoreAllMocks();
  saveFailedPlanningFeedbackDraft(pinned, "keep");
  const key = failedPlanningFeedbackDraftKey(pinned); const bytes = window.localStorage.getItem(key);
  const original: CommandRequest = { command_type: "ReviseFailedPlanningRequest", command_id: id(20),
    workspace_id: id(1), target_type: "planning_request", target_id: id(2), actor: { type: "user", id: "human-current" },
    expected_version: 12, correlation_id: id(21), idempotency_key: id(22), payload: failedPlanningFeedbackPayload(pinned, "keep") };
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("unavailable"); });
  expect(clearAppliedFailedPlanningFeedbackDraft(pinned.scope, original).ok).toBe(false);
  expect(window.localStorage.getItem(key)).toBe(bytes);
});
