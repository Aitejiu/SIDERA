import { describe, expect, it } from "vitest";

import type { PlanningProposal, PlanningRequest, PlanningRun } from "./types";
import { detectPlanningMode, reviewInitialNativeProposal } from "./nativePlanningReview";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const REQUEST_ID = "30000000-0000-0000-0000-000000000001";
const RUN_ID = "40000000-0000-0000-0000-000000000001";
const PROFILE_ID = "50000000-0000-0000-0000-000000000001";
const HASH = "a".repeat(64);

function run(overrides: Record<string, unknown> = {}): PlanningRun {
  return {
    id: RUN_ID,
    workspace_id: WORKSPACE_ID,
    planning_request_id: REQUEST_ID,
    execution_mode: "native_profile",
    ...overrides,
  } as PlanningRun;
}

function request(overrides: Record<string, unknown> = {}): PlanningRequest {
  const activeRun = run();
  return {
    id: REQUEST_ID,
    workspace_id: WORKSPACE_ID,
    kind: "create",
    status: "proposal_ready",
    version: 8,
    current_proposal_revision: 2,
    current_proposal_content_hash: HASH,
    current_run_id: RUN_ID,
    planning_profile_id: PROFILE_ID,
    planning_profile_version: 3,
    profile: {
      id: PROFILE_ID,
      workspace_id: WORKSPACE_ID,
      execution_mode: "native_profile",
    },
    active_run: activeRun,
    ...overrides,
  } as PlanningRequest;
}

function proposal(overrides: Record<string, unknown> = {}): PlanningProposal {
  return {
    id: "60000000-0000-0000-0000-000000000001",
    planning_request_id: REQUEST_ID,
    planning_run_id: RUN_ID,
    kind: "create",
    revision: 2,
    content_hash: HASH,
    display: {
      subtasks: [{ key: "prepare", title: "Prepare" }],
      worker_items: [{ key: "inspect", subtask_key: "prepare", title: "Inspect" }],
    },
    ...overrides,
  } as unknown as PlanningProposal;
}

describe("native planning review facts", () => {
  it("uses the selected active native Run and returns its actual immutable targets", () => {
    const savedRequest = request();
    const result = reviewInitialNativeProposal({
      workspaceId: WORKSPACE_ID,
      requestId: REQUEST_ID,
      request: savedRequest,
      proposal: proposal(),
      runs: [run({ id: "70000000-0000-0000-0000-000000000001", execution_mode: "legacy" })],
    });

    expect(detectPlanningMode(savedRequest, [])).toBe("native_profile");
    expect(result).toEqual({
      ok: true,
      binding: {
        requestVersion: 8,
        proposalRevision: 2,
        proposalContentHash: HASH,
        profileId: PROFILE_ID,
        profileVersion: 3,
      },
      targets: [
        { kind: "subtask", key: "prepare", label: "Prepare", subtaskKey: null, strategy: "create", existingId: null },
        { kind: "worker_item", key: "inspect", label: "Inspect", subtaskKey: "prepare", strategy: "create", existingId: null },
      ],
    });
    if (result.ok) expect(Object.isFrozen(result.binding)).toBe(true);
  });

  it("recognizes a genuine saved legacy profile and its matching legacy Run", () => {
    expect(detectPlanningMode(
      request({
        profile: { id: PROFILE_ID, workspace_id: WORKSPACE_ID, execution_mode: "legacy" },
        active_run: run({ execution_mode: "legacy" }),
      }),
      [],
    )).toBe("legacy");
  });

  it.each([
    ["native profile with legacy Run", "native_profile", "legacy"],
    ["legacy profile with native Run", "legacy", "native_profile"],
  ] as const)("marks a %s unavailable", (_name, profileMode, runMode) => {
    expect(detectPlanningMode(
      request({
        profile: { id: PROFILE_ID, workspace_id: WORKSPACE_ID, execution_mode: profileMode },
        active_run: run({ execution_mode: runMode }),
      }),
      [],
    )).toBe("unavailable");
  });

  it("does not promote an unrelated last Run when the selected native Run is missing", () => {
    expect(detectPlanningMode(
      request({ active_run: null }),
      [run({ id: "70000000-0000-0000-0000-000000000001" })],
    )).toBe("unavailable");
  });

  const foreignCases: Array<[
    string,
    Partial<Parameters<typeof reviewInitialNativeProposal>[0]>,
    Record<string, unknown>,
  ]> = [
    ["foreign route workspace", { workspaceId: "foreign-workspace" }, {}],
    ["foreign request", { request: request({ id: "foreign-request" }) }, {}],
    ["foreign selected Run", { request: request({ active_run: run({ planning_request_id: "foreign-request" }) }) }, {}],
    ["foreign proposal", {}, { planning_request_id: "foreign-request" }],
  ];

  it.each(foreignCases)("refuses a %s without synthetic target identities", (_name, inputOverrides, proposalOverrides) => {
    const result = reviewInitialNativeProposal({
      workspaceId: WORKSPACE_ID,
      requestId: REQUEST_ID,
      request: inputOverrides.request ?? request(),
      proposal: proposal(proposalOverrides),
      runs: [],
      ...inputOverrides,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toBe("规划方案事实不可用");
  });

  it("refuses malformed target keys even when the proposal can still be displayed", () => {
    const result = reviewInitialNativeProposal({
      workspaceId: WORKSPACE_ID,
      requestId: REQUEST_ID,
      request: request(),
      proposal: proposal({ display: { subtasks: [{ title: "No key" }], worker_items: [] } }),
      runs: [],
    });

    expect(result).toEqual({ ok: false, reason: "规划方案事实不可用" });
  });
});
