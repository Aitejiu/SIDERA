import type { components } from "@/shared/api/schema";

import type { ResultActionInput } from "./api";

type TaskResultOverview = components["schemas"]["TaskResultOverviewResponse"];
type ResultAction = components["schemas"]["ResultActionResponse"];

export const RESULT_WORKSPACE_ID = "71000000-0000-0000-0000-000000000001";
export const RESULT_ROOT_TASK_ID = "71000000-0000-0000-0000-000000000002";
export const RESULT_CANDIDATE_ID = "71000000-0000-0000-0000-000000000003";

export function resultAction(commandType: "CompleteTask" | "RequestTaskRework" = "CompleteTask"): ResultAction {
  return {
    action: commandType === "CompleteTask" ? "accept_candidate" : "request_rework",
    command_type: commandType,
    target_type: "task",
    target_id: RESULT_ROOT_TASK_ID,
    expected_version: 11,
    expected_result_version: 7,
    expected_fingerprint: "c".repeat(64),
    candidate_id: RESULT_CANDIDATE_ID,
    candidate_hash: "c".repeat(64),
    plan_content_hash: "d".repeat(64),
    requires_reason: commandType === "RequestTaskRework",
  };
}

export function resultView(
  overrides: Partial<TaskResultOverview> = {},
): TaskResultOverview {
  const candidate = {
    candidate_id: RESULT_CANDIDATE_ID,
    candidate_hash: "c".repeat(64),
    plan_snapshot_id: "71000000-0000-0000-0000-000000000004",
    plan_content_hash: "d".repeat(64),
    integration_execution_id: "71000000-0000-0000-0000-000000000005",
    base_commit: "a".repeat(40),
    result_commit: "b".repeat(40),
    result_tree: null,
    source_result_ids: ["71000000-0000-0000-0000-000000000006"],
    code_result_id: "71000000-0000-0000-0000-000000000007",
    previous_candidate_id: null,
    created_at: "2026-09-08T01:00:00Z",
    is_current: true,
    artifacts: [{
      artifact_id: "71000000-0000-0000-0000-000000000008",
      artifact_type: "patch",
      content_hash: "e".repeat(64),
      size_bytes: 128,
      media_type: "text/plain",
      content_available: true,
    }],
  } satisfies components["schemas"]["ResultCandidateResponse"];

  const criterion = {
    criterion_key: "expired-credentials",
    text: "Expired credentials are rejected",
    mode: "automatic",
    check_keys: ["unit"],
    observed_status: "passed",
    verification_status: "passed",
    verification_result_ids: ["71000000-0000-0000-0000-000000000009"],
    decision: null,
    decision_reason: null,
  } satisfies components["schemas"]["ResultCriterionResponse"];

  return {
    workspace_id: RESULT_WORKSPACE_ID,
    root_task_id: RESULT_ROOT_TASK_ID,
    task_title: "修复过期凭据校验",
    task_status: "waiting",
    task_version: 11,
    result_version: 7,
    result_stage: "awaiting_acceptance",
    current_candidate: candidate,
    acceptance: null,
    apply_policy: {
      apply_on_accept: false,
      mode: "fast_forward_only",
      target_ref: null,
    },
    active_operation: null,
    criteria: [criterion],
    blocking_reasons: [],
    available_actions: [resultAction()],
    candidate_history: { items: [candidate], next_cursor: null },
    last_change_cursor: 19,
    as_of: "2026-09-08T01:05:00Z",
    ...overrides,
  } satisfies TaskResultOverview;
}

export function completeTaskInput(
  overrides: Partial<Extract<ResultActionInput, { command_type: "CompleteTask" }>["payload"]> = {},
): Extract<ResultActionInput, { command_type: "CompleteTask" }> {
  return {
    command_type: "CompleteTask",
    payload: {
      candidate_id: RESULT_CANDIDATE_ID,
      candidate_hash: "c".repeat(64),
      acceptance_basis_hash: "f".repeat(64),
      expected_result_version: 7,
      criteria: [{
        criterion_key: "expired-credentials",
        decision: "accept_passed",
        reason: null,
      }],
      ...overrides,
    },
  };
}

export function integrationSpecFixture(): Record<string, unknown> {
  return {
    schema_version: 1,
    repository_ref: "repo://sidera",
    base_commit: "a".repeat(40),
    integration_worker_item_key: "integration-worker",
    input_group_keys: ["implementation-output"],
    criteria: [{
      key: "unit-tests",
      text: "单元测试通过",
      mode: "automatic",
      check_keys: ["unit"],
    }],
    checks: [{
      key: "unit",
      version: 1,
      argv: ["uv", "run", "pytest"],
      cwd: ".",
      environment_ref: "env://local",
      prepare_argvs: [],
      timeout_seconds: 120,
      success_exit_codes: [0],
      report_paths: ["reports/unit.json"],
      required: true,
    }],
    verification_binding: {
      adapter_module_ref: "71000000-0000-0000-0000-000000000010",
      adapter_config_version: 1,
      workspace_module_ref: "71000000-0000-0000-0000-000000000011",
      workspace_config_version: 1,
    },
    rework_policy: {
      max_auto_rework_rounds: 2,
      max_auto_rework_seconds: 900,
      allowed_worker_item_keys: ["implementation-worker"],
    },
    apply_policy: {
      apply_on_accept: false,
      mode: "fast_forward_only",
      target_ref: null,
    },
    finalization_policy: "stop_unused",
  };
}
