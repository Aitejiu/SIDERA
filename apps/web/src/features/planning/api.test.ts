import { describe, expect, it } from "vitest";

import {
  buildConfirmPlanRevisionCommand,
  buildSubmitPlanRevisionCommand,
} from "./api";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const REQUEST_ID = "40000000-0000-0000-0000-000000000001";
const SNAPSHOT_ID = "50000000-0000-0000-0000-000000000001";
const PROFILE_ID = "60000000-0000-0000-0000-000000000001";

describe("plan revision API", () => {
  it("builds a submit command with only the normalized revision brief and base fence", () => {
    const command = buildSubmitPlanRevisionCommand({
      workspaceId: WORKSPACE_ID,
      rootTaskId: ROOT_TASK_ID,
      expectedVersion: 7,
      payload: {
        root_task_id: ROOT_TASK_ID,
        expected_base_snapshot_id: SNAPSHOT_ID,
        expected_base_generation: 3,
        expected_base_content_hash: "a".repeat(64),
        objective: "调整后端拆分",
        constraints: ["保持公开 API 稳定"],
        acceptance_criteria: ["现有测试保持通过"],
        planning_profile_id: PROFILE_ID,
        planning_profile_version: 2,
      },
    });

    expect(command).toMatchObject({
      workspace_id: WORKSPACE_ID,
      command_type: "SubmitPlanRevision",
      target_type: "task",
      target_id: ROOT_TASK_ID,
      expected_version: 7,
      payload: {
        root_task_id: ROOT_TASK_ID,
        expected_base_snapshot_id: SNAPSHOT_ID,
        expected_base_generation: 3,
        expected_base_content_hash: "a".repeat(64),
        objective: "调整后端拆分",
        constraints: ["保持公开 API 稳定"],
        acceptance_criteria: ["现有测试保持通过"],
        planning_profile_id: PROFILE_ID,
        planning_profile_version: 2,
      },
    });
    expect(Object.keys(command.payload)).toEqual([
      "root_task_id",
      "expected_base_snapshot_id",
      "expected_base_generation",
      "expected_base_content_hash",
      "objective",
      "constraints",
      "acceptance_criteria",
      "planning_profile_id",
      "planning_profile_version",
    ]);
  });

  it("builds an exact whole-revision confirmation command", () => {
    const command = buildConfirmPlanRevisionCommand({
      workspaceId: WORKSPACE_ID,
      requestId: REQUEST_ID,
      expectedVersion: 4,
      payload: {
        expected_proposal_revision: 2,
        expected_proposal_content_hash: "b".repeat(64),
        expected_diff_hash: "c".repeat(64),
        expected_base_snapshot_id: SNAPSHOT_ID,
        expected_base_content_hash: "a".repeat(64),
        profile_id: PROFILE_ID,
        profile_version: 2,
      },
    });

    expect(command).toMatchObject({
      workspace_id: WORKSPACE_ID,
      command_type: "ConfirmPlanRevision",
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 4,
      payload: {
        expected_proposal_revision: 2,
        expected_proposal_content_hash: "b".repeat(64),
        expected_diff_hash: "c".repeat(64),
        expected_base_snapshot_id: SNAPSHOT_ID,
        expected_base_content_hash: "a".repeat(64),
        profile_id: PROFILE_ID,
        profile_version: 2,
      },
    });
    expect(Object.keys(command.payload)).toEqual([
      "expected_proposal_revision",
      "expected_proposal_content_hash",
      "expected_diff_hash",
      "expected_base_snapshot_id",
      "expected_base_content_hash",
      "profile_id",
      "profile_version",
    ]);
  });
});
