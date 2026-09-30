export type RealBasicManifest = {
  schema_version: number;
  run_id: string;
  workspace_id: string;
  implementation_fingerprint: string;
  runtime_fingerprint: string;
  run_root: string;
  approval_ref: string;
  resource_binding_mode: string;
  main_agent_gateway_url: string;
  web_url: string;
  scenarios: Record<string, unknown>;
};

export type RealBasicGroup = "A" | "B" | "C";

export type PlanningProposalGateSnapshot = {
  proposalVisible: boolean;
  requestStatus: string | null;
  runStatus: string | null;
};

export type PlanningProposalGate = "ready" | "failed" | "blocked" | "pending";

export type RealExecutionAttemptState = "ready" | "failed" | "pending";

const REAL_BASIC_SCENARIO_MAX_MS = 3_600_000;
const REAL_BASIC_TEST_BUFFER_MS = 300_000;
const SAFE_REAL_EXECUTION_FAILURE_CODES = new Set([
  "acceptance_budget_exhausted",
  "lease_expired",
]);

export function safeRealExecutionFailureCode(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) return "none";
  return SAFE_REAL_EXECUTION_FAILURE_CODES.has(value) ? value : "other";
}

export function realBasicScenarioDeadlineMs(
  manifest: Pick<RealBasicManifest, "scenarios">,
): number {
  const scenario = manifest.scenarios["user-flow"];
  if (!isRecord(scenario)) throw new Error("real user-flow deadline is invalid");
  const seconds = Number(scenario.max_seconds);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    throw new Error("real user-flow deadline is invalid");
  }
  return Math.min(seconds * 1_000, REAL_BASIC_SCENARIO_MAX_MS);
}

export function realBasicBrowserTimeoutMs(
  manifest: Pick<RealBasicManifest, "scenarios">,
): number {
  return realBasicScenarioDeadlineMs(manifest) + REAL_BASIC_TEST_BUFFER_MS;
}

export function latestRealExecutionAttempts(
  executions: readonly Record<string, unknown>[],
): Record<string, unknown>[] {
  const latestByLineage = new Map<string, Record<string, unknown>>();
  for (const [index, execution] of executions.entries()) {
    const lineage = stringValue(execution.lineage_id) || stringValue(execution.id) || `missing-${index}`;
    const existing = latestByLineage.get(lineage);
    if (existing === undefined || compareExecutionAttempt(execution, existing) > 0) {
      latestByLineage.set(lineage, execution);
    }
  }
  return [...latestByLineage.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([, execution]) => execution);
}

export function realTaskExecutions(
  executions: readonly Record<string, unknown>[],
  taskIds: readonly string[],
): Record<string, unknown>[] {
  const taskIdSet = new Set(taskIds);
  return executions.filter((execution) => (
    typeof execution.task_id === "string" && taskIdSet.has(execution.task_id)
  ));
}

export function isRealSourceExecutionPurpose(purpose: string): boolean {
  const normalized = purpose.toLowerCase();
  if (normalized.includes("integration") || normalized.includes("summary.json")) {
    return false;
  }
  return normalized.includes("source-a") || normalized.includes("source-b");
}

export function classifyRealExecutionAttempts(
  executions: readonly Record<string, unknown>[],
): RealExecutionAttemptState {
  const latest = latestRealExecutionAttempts(executions);
  if (latest.length === 0) return "pending";
  if (latest.some((execution) => (
    execution.status === "failed"
    || execution.status === "canceled"
    || execution.status === "lost"
  ))) {
    return "failed";
  }
  if (latest.every((execution) => execution.status === "succeeded")) return "ready";
  return "pending";
}

function compareExecutionAttempt(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): number {
  const leftAttempt = Number.isInteger(left.attempt) ? Number(left.attempt) : 0;
  const rightAttempt = Number.isInteger(right.attempt) ? Number(right.attempt) : 0;
  if (leftAttempt !== rightAttempt) return leftAttempt - rightAttempt;
  const leftCreated = stringValue(left.created_at);
  const rightCreated = stringValue(right.created_at);
  if (leftCreated !== rightCreated) return leftCreated.localeCompare(rightCreated);
  return stringValue(left.id).localeCompare(stringValue(right.id));
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export type RealBasicBoundaryInput = {
  env: Readonly<Record<string, string | undefined>>;
  manifest: unknown;
  approval: unknown;
  groups: Readonly<Record<string, unknown>>;
  requiredGroups?: readonly RealBasicGroup[];
  selectedGroup?: RealBasicGroup;
};

export class RealBasicBoundaryError extends Error {
  constructor(code: string) {
    super(code);
    this.name = "RealBasicBoundaryError";
  }
}

export function requireRealAcceptanceGroup(
  value: string | undefined,
  expected: RealBasicGroup,
): RealBasicGroup {
  if (value === undefined || value.trim() === "") {
    throw new RealBasicBoundaryError("real_group_selection_required");
  }
  if (value !== expected) {
    throw new RealBasicBoundaryError("real_group_flow_mismatch");
  }
  return expected;
}

export function decodedLastPathSegment(value: string): string {
  const segment = new URL(value).pathname.split("/").filter(Boolean).at(-1) ?? "";
  return decodeURIComponent(segment);
}

export function classifyPlanningProposalGate(
  snapshot: PlanningProposalGateSnapshot,
): PlanningProposalGate {
  if (snapshot.requestStatus === "blocked" || snapshot.runStatus === "blocked") {
    return "blocked";
  }
  if (snapshot.requestStatus === "failed" || snapshot.runStatus === "failed") {
    return "failed";
  }
  if (snapshot.requestStatus === "proposal_ready" && snapshot.proposalVisible) {
    return "ready";
  }
  return "pending";
}

export function validateRealBasicBrowserBoundary(
  input: RealBasicBoundaryInput,
): RealBasicManifest {
  if (input.env.SIDERA_RUN_REAL_BASIC !== "1") {
    throw new RealBasicBoundaryError("real_opt_in_required");
  }
  if (input.env.SIDERA_RUN_REAL_BASIC_BROWSER !== "1") {
    throw new RealBasicBoundaryError("real_browser_opt_in_required");
  }
  const manifest = requireManifest(input.manifest);
  if (manifest.schema_version !== 2) {
    throw new RealBasicBoundaryError("real_manifest_schema_required");
  }
  if (manifest.resource_binding_mode !== "persisted") {
    throw new RealBasicBoundaryError("real_resource_binding_mode_required");
  }
  if (!manifest.scenarios.profile || !manifest.scenarios["user-flow"]) {
    throw new RealBasicBoundaryError("real_scenarios_incomplete");
  }
  if (input.selectedGroup === "B" && !manifest.scenarios.revision) {
    throw new RealBasicBoundaryError("real_revision_scenario_required");
  }
  if (!isLoopbackUrl(manifest.main_agent_gateway_url) || !isLoopbackUrl(manifest.web_url)) {
    throw new RealBasicBoundaryError("real_endpoints_not_loopback");
  }
  if (input.env.SIDERA_BASIC_WORKSPACE_ID !== undefined
    && input.env.SIDERA_BASIC_WORKSPACE_ID !== manifest.workspace_id) {
    throw new RealBasicBoundaryError("real_workspace_binding_mismatch");
  }
  if (input.env.SIDERA_MAIN_AGENT_GATEWAY_URL !== undefined
    && input.env.SIDERA_MAIN_AGENT_GATEWAY_URL.replace(/\/$/, "")
      !== manifest.main_agent_gateway_url.replace(/\/$/, "")) {
    throw new RealBasicBoundaryError("real_gateway_binding_mismatch");
  }
  if (input.env.SIDERA_BASIC_WEB_URL !== undefined
    && input.env.SIDERA_BASIC_WEB_URL.replace(/\/$/, "")
      !== manifest.web_url.replace(/\/$/, "")) {
    throw new RealBasicBoundaryError("real_web_binding_mismatch");
  }

  const approval = requireRecord(input.approval, "real_approval_invalid");
  if (approval.approved !== true) {
    throw new RealBasicBoundaryError("real_approval_required");
  }
  if (approval.run_id !== manifest.run_id || approval.workspace_id !== manifest.workspace_id) {
    throw new RealBasicBoundaryError("real_approval_identity_mismatch");
  }
  if (approval.implementation_fingerprint !== manifest.implementation_fingerprint
    || approval.runtime_fingerprint !== manifest.runtime_fingerprint) {
    throw new RealBasicBoundaryError("real_approval_identity_mismatch");
  }

  const requiredGroups = input.requiredGroups ?? ["A", "B", "C"];
  if (new Set(requiredGroups).size !== requiredGroups.length) {
    throw new RealBasicBoundaryError("real_group_selection_invalid");
  }
  for (const group of requiredGroups) {
    if (!(group === "A" || group === "B" || group === "C")) {
      throw new RealBasicBoundaryError("real_group_selection_invalid");
    }
  }

  const rootTaskIds = new Set<string>();
  for (const group of requiredGroups) {
    const observation = requireRecord(input.groups[group], "real_group_observation_missing");
    if (observation.group !== group) {
      throw new RealBasicBoundaryError("real_group_binding_mismatch");
    }
    if (observation.run_id !== manifest.run_id
      || observation.workspace_id !== manifest.workspace_id
      || observation.implementation_fingerprint !== manifest.implementation_fingerprint
      || observation.runtime_fingerprint !== manifest.runtime_fingerprint) {
      throw new RealBasicBoundaryError("real_group_identity_mismatch");
    }
    if (observation.status !== "completed" || !isUuid(observation.root_task_id)) {
      throw new RealBasicBoundaryError("real_group_observation_incomplete");
    }
    if (rootTaskIds.has(observation.root_task_id)) {
      throw new RealBasicBoundaryError("real_group_root_not_unique");
    }
    rootTaskIds.add(observation.root_task_id);
  }
  return manifest;
}

function requireManifest(value: unknown): RealBasicManifest {
  const record = requireRecord(value, "real_manifest_invalid");
  const runId = stringField(record, "run_id", "real_manifest_invalid");
  const workspaceId = stringField(record, "workspace_id", "real_manifest_invalid");
  const implementationFingerprint = stringField(
    record,
    "implementation_fingerprint",
    "real_manifest_invalid",
  );
  const runtimeFingerprint = stringField(
    record,
    "runtime_fingerprint",
    "real_manifest_invalid",
  );
  const runRoot = stringField(record, "run_root", "real_manifest_invalid");
  const approvalRef = stringField(record, "approval_ref", "real_manifest_invalid");
  const resourceBindingMode = stringField(
    record,
    "resource_binding_mode",
    "real_manifest_invalid",
  );
  const gatewayUrl = stringField(record, "main_agent_gateway_url", "real_manifest_invalid");
  const webUrl = stringField(record, "web_url", "real_manifest_invalid");
  if (!isUuid(runId) || !isUuid(workspaceId)) {
    throw new RealBasicBoundaryError("real_manifest_identity_invalid");
  }
  if (!/^[0-9a-f]{64}$/i.test(implementationFingerprint)
    || !/^[0-9a-f]{64}$/i.test(runtimeFingerprint)
    || !runRoot.startsWith("/")
    || !approvalRef.startsWith("/")) {
    throw new RealBasicBoundaryError("real_manifest_identity_invalid");
  }
  if (!isRecord(record.scenarios)) {
    throw new RealBasicBoundaryError("real_scenarios_invalid");
  }
  return {
    schema_version: typeof record.schema_version === "number" ? record.schema_version : 0,
    run_id: runId,
    workspace_id: workspaceId,
    implementation_fingerprint: implementationFingerprint,
    runtime_fingerprint: runtimeFingerprint,
    run_root: runRoot,
    approval_ref: approvalRef,
    resource_binding_mode: resourceBindingMode,
    main_agent_gateway_url: gatewayUrl,
    web_url: webUrl,
    scenarios: record.scenarios,
  };
}

function requireRecord(value: unknown, code: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new RealBasicBoundaryError(code);
  }
  return value;
}

function stringField(value: Record<string, unknown>, key: string, code: string): string {
  const field = value[key];
  if (typeof field !== "string" || field === "") {
    throw new RealBasicBoundaryError(code);
  }
  return field;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isUuid(value: unknown): value is string {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function isLoopbackUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:"
      && (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost" || parsed.hostname === "[::1]")
      && parsed.username === ""
      && parsed.password === ""
      && parsed.pathname === "/"
      && parsed.search === ""
      && parsed.hash === "";
  } catch {
    return false;
  }
}
