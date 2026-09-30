import { describe, expect, it } from "vitest";

import * as boundary from "./basicCapabilityRealBoundary";
import {
  RealBasicBoundaryError,
  classifyRealExecutionAttempts,
  latestRealExecutionAttempts,
  realBasicBrowserTimeoutMs,
  realBasicScenarioDeadlineMs,
  requireRealAcceptanceGroup,
  safeRealExecutionFailureCode,
  validateRealBasicBrowserBoundary,
} from "./basicCapabilityRealBoundary";

const IDS = {
  run: "10000000-0000-4000-8000-000000000001",
  workspace: "20000000-0000-4000-8000-000000000001",
  implementation: "a".repeat(64),
  runtime: "b".repeat(64),
};

type TestBoundaryInput = {
  env: Record<string, string | undefined>;
  manifest: Record<string, unknown>;
  approval: Record<string, unknown>;
  groups: Record<string, unknown>;
};

function input(): TestBoundaryInput {
  const manifest = {
    schema_version: 2,
    run_id: IDS.run,
    workspace_id: IDS.workspace,
    implementation_fingerprint: IDS.implementation,
    runtime_fingerprint: IDS.runtime,
    run_root: "/tmp/sidera-run",
    approval_ref: "/tmp/sidera-run/approval.json",
    resource_binding_mode: "persisted",
    main_agent_gateway_url: "http://127.0.0.1:18113/",
    web_url: "http://127.0.0.1:18114/",
    scenarios: { profile: {}, "user-flow": {} },
  };
  return {
    env: {
      SIDERA_RUN_REAL_BASIC: "1",
      SIDERA_RUN_REAL_BASIC_BROWSER: "1",
      SIDERA_BASIC_WORKSPACE_ID: IDS.workspace,
      SIDERA_MAIN_AGENT_GATEWAY_URL: "http://127.0.0.1:18113",
      SIDERA_BASIC_WEB_URL: "http://127.0.0.1:18114",
    },
    manifest,
    approval: {
      approved: true,
      run_id: IDS.run,
      workspace_id: IDS.workspace,
      implementation_fingerprint: IDS.implementation,
      runtime_fingerprint: IDS.runtime,
    },
    groups: {
      A: group("A", "30000000-0000-4000-8000-000000000001"),
      B: group("B", "30000000-0000-4000-8000-000000000002"),
      C: group("C", "30000000-0000-4000-8000-000000000003"),
    },
  };
}

function group(groupName: string, rootTaskId: string) {
  return {
    group: groupName,
    run_id: IDS.run,
    workspace_id: IDS.workspace,
    implementation_fingerprint: IDS.implementation,
    runtime_fingerprint: IDS.runtime,
    root_task_id: rootTaskId,
    status: "completed",
  };
}

describe("real basic browser boundary", () => {
  it("decodes encoded route segments before using them as API identifiers", () => {
    const decodeLastPathSegment = Reflect.get(boundary, "decodedLastPathSegment");

    expect(decodeLastPathSegment).toBeTypeOf("function");
    if (typeof decodeLastPathSegment !== "function") return;

    expect(decodeLastPathSegment(
      "http://127.0.0.1:65527/workspaces/example/chat/programmatic%3Athread-id",
    )).toBe("programmatic:thread-id");
    expect(decodeLastPathSegment(
      "http://127.0.0.1:65527/workspaces/example/tasks/20000000-0000-4000-8000-000000000001",
    )).toBe("20000000-0000-4000-8000-000000000001");
  });

  it("recognizes source worker purposes without requiring a filename suffix", () => {
    const isSourcePurpose = Reflect.get(boundary, "isRealSourceExecutionPurpose");

    expect(isSourcePurpose).toBeTypeOf("function");
    if (typeof isSourcePurpose !== "function") return;

    expect(isSourcePurpose("并行创建 source-a 成果文件")).toBe(true);
    expect(isSourcePurpose("并行创建 source-b 成果文件")).toBe(true);
    expect(isSourcePurpose("生成 out/source-a.json")).toBe(true);
    expect(isSourcePurpose("运行集成验收")).toBe(false);
    expect(isSourcePurpose(
      "Read only selected out/source-a.json and out/source-b.json, then create out/summary.json",
    )).toBe(false);
  });

  it("includes child-task executions while excluding unrelated workspace executions", () => {
    const forCurrentTaskTree = Reflect.get(boundary, "realTaskExecutions");

    expect(forCurrentTaskTree).toBeTypeOf("function");
    if (typeof forCurrentTaskTree !== "function") return;

    const rootExecution = { id: "root", task_id: "root-current" };
    const subtaskAExecution = { id: "subtask-a", task_id: "subtask-a" };
    const subtaskBExecution = { id: "subtask-b", task_id: "subtask-b" };
    const unrelatedSubtaskExecution = { id: "previous", task_id: "subtask-previous" };
    const unbound = { id: "unbound", task_id: null };

    expect(forCurrentTaskTree(
      [rootExecution, subtaskAExecution, subtaskBExecution, unrelatedSubtaskExecution, unbound],
      ["root-current", "subtask-a", "subtask-b"],
    )).toEqual([rootExecution, subtaskAExecution, subtaskBExecution]);
  });

  it("exposes only stable failure codes in acceptance diagnostics", () => {
    expect(safeRealExecutionFailureCode("lease_expired")).toBe("lease_expired");
    expect(safeRealExecutionFailureCode("acceptance_budget_exhausted")).toBe(
      "acceptance_budget_exhausted",
    );
    expect(safeRealExecutionFailureCode("sk-live-tokenvalue123")).toBe("other");
    expect(safeRealExecutionFailureCode("provider error token=secret123")).toBe(
      "other",
    );
    expect(safeRealExecutionFailureCode(null)).toBe("none");
  });

  it("treats a terminal current attempt as failure, not as an endless wait", () => {
    const result = [
      execution("integration-old", "integration-lineage", 1, "lost", "lease_expired"),
      execution("integration-current", "integration-lineage", 2, "failed", "acceptance_budget_exhausted"),
    ];

    expect(classifyRealExecutionAttempts(result)).toBe("failed");
  });

  it("keeps waiting when a lost attempt has a newer live attempt", () => {
    const result = [
      execution("integration-old", "integration-lineage", 1, "lost", "lease_expired"),
      execution("integration-current", "integration-lineage", 2, "running", null),
    ];

    expect(latestRealExecutionAttempts(result).map((item) => item.id)).toEqual([
      "integration-current",
    ]);
    expect(classifyRealExecutionAttempts(result)).toBe("pending");
  });

  it("uses the latest attempt per lineage and reports ready only when all latest attempts succeed", () => {
    const result = [
      execution("source-a-old", "source-a", 1, "lost", "lease_expired"),
      execution("source-a-current", "source-a", 2, "succeeded", null),
      execution("source-b-current", "source-b", 1, "succeeded", null),
      execution("integration-current", "integration", 1, "succeeded", null),
    ];

    expect(classifyRealExecutionAttempts(result)).toBe("ready");
    expect(latestRealExecutionAttempts(result).map((item) => item.id)).toEqual([
      "integration-current",
      "source-a-current",
      "source-b-current",
    ]);
  });

  it("classifies the planning proposal gate from request and run state", () => {
    const classify = Reflect.get(
      boundary,
      "classifyPlanningProposalGate",
    ) as (snapshot: {
      proposalVisible: boolean;
      requestStatus: string | null;
      runStatus: string | null;
    }) => string;

    expect(classify).toBeTypeOf("function");
    expect(classify({
      proposalVisible: true,
      requestStatus: "proposal_ready",
      runStatus: "succeeded",
    })).toBe("ready");
    expect(classify({
      proposalVisible: false,
      requestStatus: "failed",
      runStatus: "failed",
    })).toBe("failed");
    expect(classify({
      proposalVisible: false,
      requestStatus: "planning",
      runStatus: "failed",
    })).toBe("failed");
    expect(classify({
      proposalVisible: false,
      requestStatus: "blocked",
      runStatus: "blocked",
    })).toBe("blocked");
    expect(classify({
      proposalVisible: false,
      requestStatus: "planning",
      runStatus: "running",
    })).toBe("pending");
  });

  it("keeps the browser timeout beyond the manifest scenario deadline", () => {
    const manifest = input().manifest;
    manifest.scenarios = { profile: {}, "user-flow": { max_seconds: 900 } };
    const typed = validateRealBasicBrowserBoundary({
      ...input(),
      manifest,
    });

    expect(realBasicScenarioDeadlineMs(typed)).toBe(900_000);
    expect(realBasicBrowserTimeoutMs(typed)).toBe(1_200_000);
  });

  it("preserves a one-hour scenario deadline and its browser cleanup buffer", () => {
    const manifest = { scenarios: { "user-flow": { max_seconds: 3600 } } };
    expect(realBasicScenarioDeadlineMs(manifest)).toBe(3_600_000);
    expect(realBasicBrowserTimeoutMs(manifest)).toBe(3_900_000);
  });

  it("accepts only explicitly opted-in, approved and A/B/C-bound manifests", () => {
    expect(validateRealBasicBrowserBoundary(input()).workspace_id).toBe(IDS.workspace);
  });

  it("binds an acceptance group label to the browser journey that actually ran", () => {
    expect(requireRealAcceptanceGroup("A", "A")).toBe("A");
    expect(requireRealAcceptanceGroup("B", "B")).toBe("B");
    expect(() => requireRealAcceptanceGroup("B", "A")).toThrow(
      new RealBasicBoundaryError("real_group_flow_mismatch"),
    );
    expect(() => requireRealAcceptanceGroup("C", "A")).toThrow(
      new RealBasicBoundaryError("real_group_flow_mismatch"),
    );
  });

  it("rejects a real browser journey with no selected acceptance group", () => {
    expect(() => requireRealAcceptanceGroup(undefined, "A")).toThrow(
      new RealBasicBoundaryError("real_group_selection_required"),
    );
    expect(() => requireRealAcceptanceGroup("  ", "A")).toThrow(
      new RealBasicBoundaryError("real_group_selection_required"),
    );
  });

  it("accepts an ordered prefix while a side-effect group is being collected", () => {
    const first = input();
    first.groups = {};
    expect(validateWithRequiredGroups(first, [])).toMatchObject({
      run_id: IDS.run,
      workspace_id: IDS.workspace,
    });

    const second = input();
    second.groups = { A: group("A", "30000000-0000-4000-8000-000000000001") };
    expect(validateWithRequiredGroups(second, ["A"])).toMatchObject({
      run_id: IDS.run,
      workspace_id: IDS.workspace,
    });

    const missingPredecessor = input();
    missingPredecessor.groups = {
      A: group("A", "30000000-0000-4000-8000-000000000001"),
    };
    expect(() => validateWithRequiredGroups(missingPredecessor, ["A", "B"])).toThrow(
      new RealBasicBoundaryError("real_group_observation_missing"),
    );
  });

  it("requires the dedicated revision budget before selecting the B journey", () => {
    const missingRevisionScenario = input();
    expect(() => validateRealBasicBrowserBoundary({
      ...missingRevisionScenario,
      selectedGroup: "B",
      requiredGroups: ["A"],
    })).toThrow(new RealBasicBoundaryError("real_revision_scenario_required"));

    const configuredRevisionScenario = input();
    configuredRevisionScenario.manifest.scenarios = {
      ...configuredRevisionScenario.manifest.scenarios as Record<string, unknown>,
      revision: {},
    };
    expect(validateRealBasicBrowserBoundary({
      ...configuredRevisionScenario,
      selectedGroup: "B",
      requiredGroups: ["A"],
    }).workspace_id).toBe(IDS.workspace);
  });

  it.each([
    ["SIDERA_RUN_REAL_BASIC", "real_opt_in_required"],
    ["SIDERA_RUN_REAL_BASIC_BROWSER", "real_browser_opt_in_required"],
  ] as const)("rejects without %s", (variable, code) => {
    const value = input();
    delete value.env[variable];
    expect(() => validateRealBasicBrowserBoundary(value)).toThrow(
      new RealBasicBoundaryError(code),
    );
  });

  it("rejects approval identity and group identity mismatches", () => {
    const approvalMismatch = input();
    approvalMismatch.approval = { ...approvalMismatch.approval, workspace_id: IDS.run };
    expect(() => validateRealBasicBrowserBoundary(approvalMismatch)).toThrow(
      new RealBasicBoundaryError("real_approval_identity_mismatch"),
    );

    const groupMismatch = input();
    groupMismatch.groups.B = group("A", "30000000-0000-4000-8000-000000000002");
    expect(() => validateRealBasicBrowserBoundary(groupMismatch)).toThrow(
      new RealBasicBoundaryError("real_group_binding_mismatch"),
    );
  });

  it.each([
    ["implementation_fingerprint", "c".repeat(64)],
    ["runtime_fingerprint", "d".repeat(64)],
  ] as const)("rejects a predecessor group with a different %s", (key, value) => {
    const mismatch = input();
    mismatch.groups.A = {
      ...(mismatch.groups.A as Record<string, unknown>),
      [key]: value,
    };

    expect(() => validateWithRequiredGroups(mismatch, ["A"])).toThrow(
      new RealBasicBoundaryError("real_group_identity_mismatch"),
    );
  });

  it("rejects a predecessor group with a missing runtime fingerprint", () => {
    const missing = input();
    delete (missing.groups.A as Record<string, unknown>).runtime_fingerprint;

    expect(() => validateWithRequiredGroups(missing, ["A"])).toThrow(
      new RealBasicBoundaryError("real_group_identity_mismatch"),
    );
  });

  it("rejects incomplete or duplicate group roots", () => {
    const incomplete = input();
    incomplete.groups.C = {
      ...(incomplete.groups.C as Record<string, unknown>),
      status: "unknown",
    };
    expect(() => validateRealBasicBrowserBoundary(incomplete)).toThrow(
      new RealBasicBoundaryError("real_group_observation_incomplete"),
    );

    const duplicate = input();
    duplicate.groups.C = group("C", "30000000-0000-4000-8000-000000000001");
    expect(() => validateRealBasicBrowserBoundary(duplicate)).toThrow(
      new RealBasicBoundaryError("real_group_root_not_unique"),
    );
  });
});

function execution(
  id: string,
  lineageId: string,
  attempt: number,
  status: string,
  failureCode: string | null,
): Record<string, unknown> {
  return {
    id,
    lineage_id: lineageId,
    attempt,
    created_at: `2026-09-25T00:00:0${attempt}Z`,
    status,
    failure_code: failureCode,
  };
}

function validateWithRequiredGroups(
  value: ReturnType<typeof input>,
  requiredGroups: readonly string[],
) {
  return validateRealBasicBrowserBoundary({
    ...value,
    requiredGroups,
  } as Parameters<typeof validateRealBasicBrowserBoundary>[0]);
}
