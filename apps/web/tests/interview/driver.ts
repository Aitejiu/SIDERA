import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, type Page } from "@playwright/test";

export type Scenario =
  | "mainline"
  | "revision"
  | "handoff"
  | "rework"
  | "recovery"
  | "knowledge"
  | "governance"
  | "controls"
  | "history";

export type InterviewManifest = {
  schema_version: 1 | 2;
  mode: "interview-demo";
  run_id: string;
  workspace_id: string;
  scenario: Scenario;
  run_root: string;
  repository: string;
  base_commit: string;
  code_commit: string;
  implementation_fingerprint?: string;
  fingerprint_version?: number;
  database_name: string;
  knowledge_database_name: string;
  pg_host: "127.0.0.1";
  pg_port: number;
  pg_user: string;
  endpoints: {
    control_plane: string;
    knowledge: string;
    main_agent: string;
    web: string;
  };
  module_ids: Record<string, string>;
  worker_ids: Record<string, string>;
  planning_profile_id: string;
  planning_profile_version: number;
  execution_deadline_seconds: number;
};

export type InterviewFacts = {
  rootTaskId: string | null;
  planGeneration: number | null;
  workerItems: Record<string, {
    id: string;
    executionId: string | null;
    sessionId: string | null;
    inputIds: string[];
    artifactIds: string[];
  }>;
  parallelObserved: boolean;
  candidateId: string | null;
  candidateHash: string | null;
  verificationCandidateId: string | null;
  verificationStatus: string | null;
  acceptedCandidateId: string | null;
  completed: boolean;
};

type JsonRecord = Record<string, unknown>;

const repositoryRoot = resolve(import.meta.dirname, "../../../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const pythonExecutable = process.env.SIDERA_E2E_PYTHON
  ?? resolve(controlPlaneRoot, ".venv/bin/python");
const demoModule = "sidera_control_plane.demo.cli";
const demoEnvironment = {
  ...process.env,
  SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL:
    process.env.SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL
    ?? `postgresql+psycopg://${process.env.USER ?? "aitejiu"}@127.0.0.1:55432/postgres`,
};
const registeredRestartComponents = new Set([
  "knowledge-service",
  "context-package-worker",
  "gateway-worker",
  "result-worker",
  "main-agent-gateway",
  "demo-notification-sink",
]);

export class InterviewDriver {
  readonly manifest: InterviewManifest;

  constructor(
    private readonly page: Page,
    manifest: InterviewManifest,
  ) {
    this.manifest = manifest;
  }

  async openChat(): Promise<void> {
    await this.page.goto(this.url(`/workspaces/${this.manifest.workspace_id}/chat`));
    const newThread = this.page.getByRole("button", { name: "新建对话", exact: true }).first();
    if (await newThread.count() > 0) {
      await newThread.click();
      await this.page.waitForURL(/\/chat\/[^/]+$/);
    }
  }

  async sendRequirement(text: string): Promise<void> {
    await this.page.getByRole("textbox", { name: "给 Main Agent 的消息", exact: true }).fill(text);
    await this.page.getByRole("button", { name: "发送消息", exact: true }).click();
    await this.page.getByText("需求已提交", { exact: false }).waitFor({ state: "visible" });
  }

  async openProposal(): Promise<void> {
    const objectLink = this.page.getByRole("link", { name: /查看对象/ }).first();
    if (await objectLink.count() > 0) {
      await objectLink.click();
      await this.page.waitForURL(/\/planning\/[^/]+$/);
      return;
    }
    const requests = asRecord(await this.getJson(
      `/api/v1/workspaces/${this.manifest.workspace_id}/planning-requests`,
    ));
    const items = asArray(requests.items);
    const request = items.at(-1);
    const requestId = asString(asRecord(request).id);
    if (requestId === null) {
      throw new Error("interview planning request link is missing");
    }
    await this.page.goto(this.url(`/workspaces/${this.manifest.workspace_id}/planning/${requestId}`));
  }

  async confirmPlan(): Promise<void> {
    await this.page.getByRole("button", { name: "确认并创建任务", exact: true }).click();
    await this.page.waitForURL(/\/tasks\/[0-9a-f-]{36}$/i);
  }

  async openLatestRevisionProposal(): Promise<void> {
    const facts = await this.getFacts();
    if (facts.rootTaskId === null) {
      throw new Error("interview root task is missing for revision");
    }
    const deadline = Date.now() + 30_000;
    let revision: JsonRecord | undefined;
    while (Date.now() < deadline) {
      const requests = asRecord(await this.getJson(
        `/api/v1/workspaces/${this.manifest.workspace_id}/planning-requests`,
      ));
      const items = asArray(requests.items).map(asRecord).reverse();
      revision = items.find((item) => asString(item.kind) === "revision") ?? items[0];
      const status = asString(revision?.status);
      if (status === "proposal_ready" || status === "conflicted") break;
      await this.page.waitForTimeout(500);
    }
    const requestId = asString(revision?.id);
    if (requestId === null) {
      throw new Error("interview revision request link is missing");
    }
    await this.page.goto(this.url(
      `/workspaces/${this.manifest.workspace_id}/tasks/${facts.rootTaskId}/revisions/${requestId}`,
    ));
    await this.page.getByRole("heading", { name: "调整任务计划", exact: true }).waitFor({ state: "visible" });
    await this.page.getByRole("heading", { name: "影响范围与同步策略", exact: true }).waitFor({ state: "visible" });
  }

  async confirmRevision(): Promise<void> {
    await this.page.getByRole("button", { name: "确认并启用新计划", exact: true }).click();
    await this.page.getByText(/新计划已提交|新计划已启用/, { exact: false }).first().waitFor({ state: "visible" });
  }

  async openRoot(): Promise<void> {
    const facts = await this.getFacts();
    if (facts.rootTaskId === null) {
      throw new Error("interview root task has not been materialized");
    }
    await this.page.goto(this.url(`/workspaces/${this.manifest.workspace_id}/tasks/${facts.rootTaskId}`));
  }

  async openSubtask(key: string): Promise<void> {
    const facts = await this.getFacts();
    if (facts.rootTaskId === null) throw new Error("interview root task is missing");
    await this.page.goto(this.url(
      `/workspaces/${this.manifest.workspace_id}/tasks/${facts.rootTaskId}/subtasks/${encodeURIComponent(normalizeSubtaskKey(key))}`,
    ));
  }

  async openWorker(key: string): Promise<void> {
    const facts = await this.getFacts();
    const resolvedKey = workerItemKeyCandidates(key).find(
      (candidate) => facts.workerItems[candidate] !== undefined,
    );
    const item = resolvedKey === undefined ? undefined : facts.workerItems[resolvedKey];
    if (item === undefined) {
      throw new Error(`interview WorkerItem is missing: ${key}`);
    }
    await this.page.goto(this.url(
      `/workspaces/${this.manifest.workspace_id}/worker-items/${item.id}`,
    ));
  }

  async openAttention(key: string): Promise<void> {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      const facts = await this.getFacts();
      const resolvedKey = workerItemKeyCandidates(key).find(
        (candidate) => facts.workerItems[candidate] !== undefined,
      );
      const targetWorkerItemId = resolvedKey === undefined
        ? null
        : facts.workerItems[resolvedKey]?.id ?? null;
      if (facts.rootTaskId !== null) {
        const graph = asRecord(await this.getJson(
          `/api/v1/tasks/${facts.rootTaskId}/graph?workspace_id=${this.manifest.workspace_id}`,
        ));
        const overview = asRecord(graph.overview);
        const attention = asArray(overview.attention_items)
          .map(asRecord)
          .find((item) => {
            const workerItemId = asString(item.worker_item_id);
            return workerItemId !== null && targetWorkerItemId === workerItemId;
          });
        const attentionWorkerItemId = asString(attention?.worker_item_id);
        if (attentionWorkerItemId !== null) {
          await this.page.goto(this.url(
            `/workspaces/${this.manifest.workspace_id}/worker-items/${attentionWorkerItemId}`,
          ));
          return;
        }
      }
      await this.page.waitForTimeout(500);
    }
    throw new Error(`interview attention projection is missing: ${key}`);
  }

  async sendInput(text: string): Promise<void> {
    const workerItemId = this.page.url().match(/\/worker-items\/([^/?#]+)/)?.[1] ?? null;
    if (workerItemId === null) {
      throw new Error("interview input requires a WorkerItem route");
    }
    const before = await this.getFacts();
    const target = Object.values(before.workerItems).find((item) => item.id === workerItemId);
    if (target?.sessionId === null || target === undefined) {
      throw new Error("interview input target Session is missing");
    }
    const previousInputIds = new Set(target.inputIds);
    const input = this.page.getByRole("textbox", { name: "介入这个 Worker", exact: true });
    const confirmContext = this.page.getByRole("button", { name: "确认当前 context", exact: true });
    if (await input.isDisabled() && await confirmContext.isVisible().catch(() => false)) {
      await confirmContext.click();
    }
    await input.fill(text);
    const sendButton = this.page.getByRole("button", { name: "发送到当前 Session", exact: true });
    await expect.poll(
      async () => await sendButton.isEnabled() || await confirmContext.isVisible().catch(() => false),
      { timeout: 15_000 },
    ).toBe(true);
    if (await confirmContext.isVisible().catch(() => false)) {
      await confirmContext.click();
    }
    await expect(sendButton).toBeEnabled({ timeout: 15_000 });
    await sendButton.click();
    await this.page.getByRole("status").filter({ hasText: "已接受" }).waitFor({ state: "visible" });
    await this.waitForInputSettlement(target.sessionId, previousInputIds);
  }

  async openTrace(): Promise<void> {
    await this.page.getByRole("button", { name: "查看当前 Agent Trace", exact: true }).click();
    await this.page.getByRole("dialog", { name: "Agent Trace", exact: true }).waitFor({ state: "visible" });
  }

  async approvePendingRelease(key: string): Promise<void> {
    const facts = await this.getFacts();
    const resolvedKey = workerItemKeyCandidates(key).find(
      (candidate) => facts.workerItems[candidate] !== undefined,
    );
    const item = resolvedKey === undefined ? undefined : facts.workerItems[resolvedKey];
    if (item === undefined) throw new Error(`interview WorkerItem is missing: ${key}`);
    await this.page.goto(this.url(
      `/workspaces/${this.manifest.workspace_id}/worker-items/${item.id}`,
    ));
    const governance = this.page.getByRole("region", { name: "协同放行控制", exact: true });
    const approve = governance.getByRole("button", { name: "批准 Proposal", exact: true });
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      if (await approve.count() > 0 && await approve.isEnabled()) {
        await approve.click();
        const dialog = this.page.getByRole("dialog", { name: "批准协同放行", exact: true });
        await dialog.waitFor({ state: "visible" });
        await dialog.getByRole("button", { name: "批准这次放行", exact: true }).click();
        await dialog.waitFor({ state: "hidden" });
        return;
      }
      const workspace = asRecord(await this.getJson(
        `/api/v1/workspaces/${this.manifest.workspace_id}/worker-items/${item.id}/workspace`,
      ));
      const collaboration = asRecord(workspace.collaboration);
      const runtime = asRecord(workspace.current_runtime);
      const collaborationState = asString(collaboration.state);
      if (
        asRecord(collaboration.active_proposal).proposal_id === undefined
        && (
          collaborationState === "released"
          || collaborationState === "materialized"
          || asString(runtime.execution_id) !== null
        )
      ) {
        return;
      }
      await this.page.reload({ waitUntil: "domcontentloaded" });
      await this.page.waitForTimeout(500);
    }
    throw new Error(`interview collaboration release proposal is missing: ${key}`);
  }

  async acceptCurrentCandidate(): Promise<void> {
    const resultsTab = this.page.getByRole("tab", { name: "成果", exact: true });
    await resultsTab.click();
    const results = this.page.getByRole("region", { name: "成果验收", exact: true });
    await results.waitFor({ state: "visible" });
    await results.getByRole("combobox").first().selectOption("accept_passed");
    await results.getByRole("button", { name: "接受成果", exact: true }).click();
    await results.getByText(/收尾中|已完成/, { exact: false }).waitFor({ state: "visible" });
  }

  async getFacts(): Promise<InterviewFacts> {
    const overviewPayload = await this.getJson(
      `/api/v1/workspaces/${this.manifest.workspace_id}/task-overviews`,
    );
    const overviews = Array.isArray(overviewPayload)
      ? overviewPayload
      : asArray(asRecord(overviewPayload).items);
    const rootTaskId = asString(asRecord(overviews[0]).root_task_id);
    const empty: InterviewFacts = {
      rootTaskId,
      planGeneration: null,
      workerItems: {},
      parallelObserved: false,
      candidateId: null,
      candidateHash: null,
      verificationCandidateId: null,
      verificationStatus: null,
      acceptedCandidateId: null,
      completed: false,
    };
    if (rootTaskId === null) return empty;

    const graph = asRecord(await this.getJson(
      `/api/v1/tasks/${rootTaskId}/graph?workspace_id=${this.manifest.workspace_id}`,
    ));
    const overview = asRecord(graph.overview);
    const planGeneration = asNumber(overview.generation);
    const workerItems: InterviewFacts["workerItems"] = {};
    const windows: Array<{ startedAt: string; finishedAt: string | null }> = [];
    const nodes = asArray(graph.nodes).map(asRecord);
    for (const node of nodes) {
      const subtaskKey = asString(node.subtask_key);
      if (subtaskKey === null) continue;
      const workspace = asRecord(await this.getJson(
        `/api/v1/tasks/${rootTaskId}/subtasks/${encodeURIComponent(subtaskKey)}?workspace_id=${this.manifest.workspace_id}`,
      ));
      for (const summaryValue of asArray(workspace.worker_items)) {
        const summary = asRecord(summaryValue);
        const stableKey = asString(summary.stable_key);
        const itemId = asString(summary.worker_item_id);
        if (stableKey === null || itemId === null) continue;
        const itemWorkspace = asRecord(await this.getJson(
          `/api/v1/workspaces/${this.manifest.workspace_id}/worker-items/${itemId}/workspace`,
        ));
        const placement = asRecord(itemWorkspace.current_placement);
        const workerItemKey = asString(placement.worker_item_key) ?? stableKey;
        const runtime = asRecord(itemWorkspace.current_runtime);
        const executionId = asString(runtime.execution_id);
        const sessionId = asString(runtime.session_id);
        const inputIds = sessionId === null ? [] : await this.readInputIds(sessionId);
        const artifactIds = asArray(itemWorkspace.artifacts)
          .map((artifact) => asString(asRecord(artifact).artifact_id))
          .filter((id): id is string => id !== null);
        workerItems[workerItemKey] = {
          id: itemId,
          executionId,
          sessionId,
          inputIds,
          artifactIds,
        };
        for (const execution of asArray(itemWorkspace.execution_history).map(asRecord)) {
          const startedAt = asString(execution.started_at);
          if (startedAt !== null) {
            windows.push({ startedAt, finishedAt: asString(execution.finished_at) });
          }
        }
      }
    }

    const result = await (async (): Promise<JsonRecord> => {
      try {
        return asRecord(await this.getJson(
          `/api/v1/workspaces/${this.manifest.workspace_id}/tasks/${rootTaskId}/results`,
        ));
      } catch {
        return {};
      }
    })();
    const candidate = asRecord(result.current_candidate);
    const candidateId = asString(candidate.candidate_id);
    const candidateHash = asString(candidate.candidate_hash);
    const criteria = asArray(result.criteria).map(asRecord);
    const statuses = criteria.map((criterion) => asString(criterion.verification_status));
    const verificationStatus = statuses.length > 0 && statuses.every((status) => status === "passed")
      ? "passed"
      : statuses.some((status) => status === "failed")
        ? "failed"
        : asString(asRecord(result.active_operation).status)
          ?? (candidateId === null ? null : "pending");
    const acceptance = asRecord(result.acceptance);
    const acceptedCandidateId = asString(acceptance.candidate_id);
    return {
      rootTaskId,
      planGeneration,
      workerItems,
      parallelObserved: overlapping(windows),
      candidateId,
      candidateHash,
      verificationCandidateId: candidateId,
      verificationStatus,
      acceptedCandidateId,
      completed: asString(result.result_stage) === "completed",
    };
  }

  async releaseGate(name: "parallel" | "before-artifact" | "handoff"): Promise<void> {
    this.runCli(["continue", "--manifest", this.manifestPath(), "--gate", name]);
  }

  async restart(component: string): Promise<void> {
    if (!registeredRestartComponents.has(component)) {
      throw new Error(`interview restart component is not registered: ${component}`);
    }
    this.runCli(["restart", "--manifest", this.manifestPath(), "--component", component]);
  }

  private async readInputIds(sessionId: string): Promise<string[]> {
    try {
      const page = asRecord(await this.getJson(
        `/api/v1/gateway/inputs?workspace_id=${this.manifest.workspace_id}&session_id=${sessionId}&limit=100`,
      ));
      return asArray(page.items)
        .map((item) => asString(asRecord(item).id))
        .filter((id): id is string => id !== null);
    } catch {
      return [];
    }
  }

  private async waitForInputSettlement(
    sessionId: string,
    previousInputIds: Set<string>,
  ): Promise<void> {
    await expect.poll(
      async () => {
        const payload = asRecord(await this.getJson(
          `/api/v1/gateway/inputs?workspace_id=${this.manifest.workspace_id}&session_id=${sessionId}&limit=100`,
        ));
        const input = asArray(payload.items)
          .map(asRecord)
          .find((value) => {
            const id = asString(value.id);
            return id !== null && !previousInputIds.has(id);
          });
        return asString(input?.status) ?? "pending";
      },
      {
        timeout: 15_000,
        message: "interview execution input did not leave the dispatch window",
      },
    ).toMatch(/^(queued|delivered|blocked|failed|cancelled|superseded)$/);
  }

  private async getJson(path: string): Promise<unknown> {
    const response = await fetch(`${this.manifest.endpoints.control_plane}${path}`);
    if (!response.ok) {
      throw new Error(`interview API GET failed: ${response.status} ${path}`);
    }
    return response.json() as Promise<unknown>;
  }

  private url(path: string): string {
    return `${this.manifest.endpoints.web}${path}`;
  }

  private manifestPath(): string {
    return resolve(this.manifest.run_root, "manifest.json");
  }

  private runCli(args: string[]): void {
    execFileSync(pythonExecutable, ["-m", demoModule, ...args], {
      cwd: controlPlaneRoot,
      env: demoEnvironment,
      stdio: "pipe",
    });
  }
}

export function readInterviewManifest(path: string): InterviewManifest {
  const value: unknown = JSON.parse(readFileSync(path, "utf8"));
  const manifest = asRecord(value);
  if (
    manifest.mode !== "interview-demo"
    || ![1, 2].includes(manifest.schema_version as number)
    || (manifest.schema_version === 2
      && (typeof manifest.implementation_fingerprint !== "string"
        || manifest.fingerprint_version !== 2))
  ) {
    throw new Error("interview manifest identity is invalid");
  }
  return manifest as unknown as InterviewManifest;
}

export function prepareInterviewRun(scenario: Scenario): string {
  const output = execFileSync(
    pythonExecutable,
    ["-m", demoModule, "prepare", "--scenario", scenario, "--print-manifest-path"],
    { cwd: controlPlaneRoot, env: demoEnvironment, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
  );
  const path = output.trim().split(/\r?\n/).at(-1);
  if (path === undefined || path === "") throw new Error("interview prepare did not return a manifest");
  return path;
}

export function startInterviewRun(manifestPath: string): void {
  execFileSync(
    pythonExecutable,
    ["-m", demoModule, "start", "--manifest", manifestPath],
    { cwd: controlPlaneRoot, env: demoEnvironment, stdio: "pipe" },
  );
}

export function stopInterviewRun(manifestPath: string): void {
  const attempts = 6;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      execFileSync(
        pythonExecutable,
        ["-m", demoModule, "stop", "--manifest", manifestPath],
        { cwd: controlPlaneRoot, env: demoEnvironment, stdio: "pipe" },
      );
      return;
    } catch (error) {
      const stderr = error instanceof Error && "stderr" in error
        ? String((error as { stderr?: Buffer | string }).stderr ?? "")
        : "";
      const retryable = stderr.includes("demo_command_failed:maintenance_busy");
      if (!retryable || attempt === attempts - 1) throw error;
      execFileSync("sleep", [String(0.1 * (attempt + 1))]);
    }
  }
}

function normalizeWorkerKey(key: string): string {
  return {
    validate: "validation",
    validation: "validation",
    reporting: "report",
    report: "report",
    review: "review-report",
    "review-report": "review-report",
    integrate: "integration",
    integration: "integration",
  }[key] ?? key;
}

function normalizeSubtaskKey(key: string): string {
  return {
    report: "reporting",
    reporting: "reporting",
  }[key] ?? key;
}

function workerItemKeyCandidates(key: string): string[] {
  return [...new Set([key, normalizeWorkerKey(key)])];
}

function overlapping(windows: Array<{ startedAt: string; finishedAt: string | null }>): boolean {
  const intervals = windows
    .map((window) => ({
      start: Date.parse(window.startedAt),
      end: window.finishedAt === null ? Date.now() : Date.parse(window.finishedAt),
    }))
    .filter((window) => Number.isFinite(window.start) && Number.isFinite(window.end) && window.end > window.start);
  for (let index = 0; index < intervals.length; index += 1) {
    for (let other = index + 1; other < intervals.length; other += 1) {
      if (Math.max(intervals[index]!.start, intervals[other]!.start)
        < Math.min(intervals[index]!.end, intervals[other]!.end)) {
        return true;
      }
    }
  }
  return false;
}

function asRecord(value: unknown): JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
