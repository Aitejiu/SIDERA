import { afterEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";

import { server } from "@/test/server";
import {
  getHandoffProposal,
  getHandoffTransition,
} from "./api";
import {
  isNativeHandoffProposal,
  isNativeHandoffTransition,
  type NativeHandoffProposal,
  type NativeHandoffTransition,
} from "./types";

const workspaceId = "20000000-0000-0000-0000-000000000001";
const workerItemId = "40000000-0000-0000-0000-000000000001";
const proposalId = "a0000000-0000-0000-0000-000000000001";
const transitionId = "c0000000-0000-0000-0000-000000000001";
const sourceProfileRef = {
  workspace_id: workspaceId,
  profile_id: "50000000-0000-0000-0000-000000000001",
  revision: 1,
};
const targetProfileRef = {
  workspace_id: workspaceId,
  profile_id: "50000000-0000-0000-0000-000000000002",
  revision: 2,
};
const context = {
  source_package_id: "b0000000-0000-0000-0000-000000000001",
  source_package_hash: "b".repeat(64),
  source_context_version: 3,
  target_package_id: null,
  target_package_hash: null,
  target_context_version: 4,
};
const planBase = {
  snapshot_id: "90000000-0000-0000-0000-000000000001",
  generation: 2,
  head_version: 6,
};

const nativeProposal: NativeHandoffProposal = {
  id: proposalId,
  proposal_schema_version: 2,
  workspace_id: workspaceId,
  root_task_id: "30000000-0000-0000-0000-000000000001",
  subtask_id: "30000000-0000-0000-0000-000000000002",
  worker_item_id: workerItemId,
  source_worker_item_version: 7,
  version: 1,
  proposal_hash: "a".repeat(64),
  status: "prepared",
  stale_reason: null,
  created_at: "2026-10-07T00:00:00Z",
  expires_at: "2026-10-07T00:15:00Z",
  confirmed_at: null,
  cancelled_at: null,
  source_execution_id: "60000000-0000-0000-0000-000000000001",
  source_session_id: "70000000-0000-0000-0000-000000000001",
  source_run_id: "80000000-0000-0000-0000-000000000001",
  source_binding_version: 2,
  source_profile_ref: sourceProfileRef,
  target_assignee: { kind: "profile", id: targetProfileRef.profile_id },
  target_profile_ref: targetProfileRef,
  target_worker_id: null,
  handoff_reason: "Change the implementation owner",
  handoff_instruction: "Continue the validation step",
  context,
  plan_base: planBase,
};

const nativeTransition: NativeHandoffTransition = {
  id: transitionId,
  handoff_schema_version: 2,
  workspace_id: workspaceId,
  worker_item_id: workerItemId,
  proposal_id: proposalId,
  version: 1,
  status: "pending",
  phase: "stop_source",
  writer_state: "source_stop_requested",
  confirmed_at: "2026-10-07T00:01:00Z",
  started_at: null,
  completed_at: null,
  failure_code: null,
  blocked_operation_id: null,
  logical_attempt: 1,
  next_attempt_at: null,
  source_execution_id: nativeProposal.source_execution_id,
  source_session_id: nativeProposal.source_session_id,
  source_run_id: nativeProposal.source_run_id,
  source_binding_version: 2,
  source_profile_ref: sourceProfileRef,
  source_input_cutoff_sequence: 5,
  target_assignee: nativeProposal.target_assignee,
  target_profile_ref: targetProfileRef,
  target_worker_id: null,
  target_execution_id: null,
  target_session_id: null,
  target_run_id: null,
  target_binding_version: null,
  target_context_version: 4,
  context,
  plan_base: planBase,
  available_recovery_strategies: [],
};

afterEach(() => server.resetHandlers());

describe("native handoff GETs", () => {
  it("returns a schema2 proposal only for its requested workspace and exact id", async () => {
    server.use(http.get(
      `/api/v1/workspaces/${workspaceId}/handoff-proposals/${proposalId}`,
      () => HttpResponse.json(nativeProposal),
    ));

    const result = await getHandoffProposal(workspaceId, proposalId);
    expect(isNativeHandoffProposal(result)).toBe(true);
    expect(result).toEqual(nativeProposal);
  });

  it("rejects foreign or malformed native-shaped proposal replies instead of treating them as legacy", async () => {
    const foreignWorkspace = "20000000-0000-0000-0000-000000000099";
    server.use(http.get(
      `/api/v1/workspaces/${workspaceId}/handoff-proposals/${proposalId}`,
      () => HttpResponse.json({ ...nativeProposal, workspace_id: foreignWorkspace }),
    ));
    await expect(getHandoffProposal(workspaceId, proposalId)).rejects.toThrow();

    server.use(http.get(
      `/api/v1/workspaces/${workspaceId}/handoff-proposals/${proposalId}`,
      () => HttpResponse.json({ ...nativeProposal, proposal_schema_version: 1 }),
    ));
    await expect(getHandoffProposal(workspaceId, proposalId)).rejects.toThrow();

    server.use(http.get(
      `/api/v1/workspaces/${workspaceId}/handoff-proposals/${proposalId}`,
      () => HttpResponse.json({ ...nativeProposal, id: "a0000000-0000-0000-0000-000000000099" }),
    ));
    await expect(getHandoffProposal(workspaceId, proposalId)).rejects.toThrow();
  });

  it("narrows a schema2 transition using its discriminator and validates requested identity", async () => {
    server.use(http.get(
      `/api/v1/workspaces/${workspaceId}/handoff-transitions/${transitionId}`,
      () => HttpResponse.json(nativeTransition),
    ));

    const result = await getHandoffTransition(workspaceId, transitionId);
    expect(isNativeHandoffTransition(result)).toBe(true);
    expect(result).toEqual(nativeTransition);

    server.use(http.get(
      `/api/v1/workspaces/${workspaceId}/handoff-transitions/${transitionId}`,
      () => HttpResponse.json({ ...nativeTransition, handoff_schema_version: 1 }),
    ));
    await expect(getHandoffTransition(workspaceId, transitionId)).rejects.toThrow();
  });
});
