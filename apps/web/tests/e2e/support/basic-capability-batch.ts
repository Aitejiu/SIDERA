import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { expect, type APIRequestContext } from "@playwright/test";

const UUID_PATTERN = /^[0-9a-f-]{36}$/i;

export type BasicCapabilityBatchNode = {
  subtask_key: string;
  task_id: string | null;
  state: string;
};

export type BasicCapabilityBatchEdge = {
  upstream_subtask_key: string;
  downstream_subtask_key: string;
  kind: string;
};

export type BasicCapabilityBatchState = {
  schema_version: 1;
  batch_id: string;
  workspace_id: string;
  planning_request_id: string;
  root_task_id: string;
  task_title: string;
  nodes: BasicCapabilityBatchNode[];
  edges: BasicCapabilityBatchEdge[];
  transport: {
    provider_calls: 0;
    desktop_non_get_requests: string[];
  };
};

export function batchStatePath(): string {
  const value = process.env.SIDERA_BASIC_BATCH_STATE_FILE;
  if (value === undefined || value.trim() === "") {
    throw new Error("SIDERA_BASIC_BATCH_STATE_FILE is required by the batch suite");
  }
  return value;
}

export function batchId(): string {
  const value = process.env.SIDERA_BASIC_BATCH_ID;
  if (value === undefined || !UUID_PATTERN.test(value)) {
    throw new Error("SIDERA_BASIC_BATCH_ID is required by the batch suite");
  }
  return value;
}

export async function readTaskGraph(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(
    `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/graph?workspace_id=${encodeURIComponent(workspaceId)}`,
  );
  expect(response.ok()).toBeTruthy();
  const value: unknown = await response.json();
  if (!isRecord(value)) {
    throw new Error("batch task graph response was not an object");
  }
  return value;
}

export async function readStableTaskGraph(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
): Promise<Record<string, unknown>> {
  let previousFingerprint: string | null = null;
  let stableObservations = 0;
  let latest: Record<string, unknown> | null = null;
  for (let attempt = 0; attempt < 16; attempt += 1) {
    latest = await readTaskGraph(request, workspaceId, rootTaskId);
    const fingerprint = JSON.stringify({
      nodes: graphNodeFacts(latest),
      edges: graphEdgeFacts(latest),
    });
    if (fingerprint === previousFingerprint) {
      stableObservations += 1;
      if (stableObservations >= 4) {
        return latest;
      }
    } else {
      stableObservations = 0;
    }
    previousFingerprint = fingerprint;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  if (latest === null) {
    throw new Error("batch task graph did not produce an observation");
  }
  throw new Error("batch task graph did not stabilize before the observation deadline");
}

export function graphNodeFacts(graph: Record<string, unknown>): BasicCapabilityBatchNode[] {
  const nodes = recordArray(graph, "nodes", "batch task graph");
  return nodes.map((node) => ({
    subtask_key: stringField(node, "subtask_key", "batch graph node"),
    task_id: nullableUuidField(node, "task_id", "batch graph node"),
    state: stringField(node, "state", "batch graph node"),
  }));
}

export function graphEdgeFacts(graph: Record<string, unknown>): BasicCapabilityBatchEdge[] {
  const edges = recordArray(graph, "edges", "batch task graph");
  return edges.map((edge) => ({
    upstream_subtask_key: stringField(edge, "upstream_subtask_key", "batch graph edge"),
    downstream_subtask_key: stringField(edge, "downstream_subtask_key", "batch graph edge"),
    kind: stringField(edge, "kind", "batch graph edge"),
  }));
}

export function graphOverviewTitle(graph: Record<string, unknown>): string {
  const overview = recordField(graph, "overview", "batch task graph");
  return stringField(overview, "title", "batch graph overview");
}

export function graphRootTaskId(graph: Record<string, unknown>): string {
  const overview = recordField(graph, "overview", "batch task graph");
  const value = stringField(overview, "root_task_id", "batch graph overview");
  if (!UUID_PATTERN.test(value)) {
    throw new Error("batch graph overview exposed an invalid root task id");
  }
  return value;
}

export function writeBatchState(state: BasicCapabilityBatchState): void {
  if (state.batch_id !== batchId()) {
    throw new Error("batch state identity does not match the current Playwright batch");
  }
  if (!UUID_PATTERN.test(state.workspace_id) || !UUID_PATTERN.test(state.root_task_id)) {
    throw new Error("batch state has invalid workspace or root task identity");
  }
  if (state.transport.provider_calls !== 0) {
    throw new Error("provider calls are forbidden in the fake batch suite");
  }
  const path = batchStatePath();
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporaryPath = `${path}.${process.pid}.tmp`;
  writeFileSync(temporaryPath, `${JSON.stringify(state, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporaryPath, path);
}

export function readBatchState(): BasicCapabilityBatchState {
  const path = batchStatePath();
  let value: unknown;
  try {
    value = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error("batch state is unreadable", { cause: error });
  }
  if (!isRecord(value) || value.schema_version !== 1) {
    throw new Error("batch state schema is invalid");
  }
  if (value.batch_id !== batchId()) {
    throw new Error("batch state belongs to a different Playwright batch");
  }
  const state = value as unknown as BasicCapabilityBatchState;
  if (!UUID_PATTERN.test(state.workspace_id) || !UUID_PATTERN.test(state.root_task_id)) {
    throw new Error("batch state identity is invalid");
  }
  return state;
}

function recordField(value: Record<string, unknown>, key: string, label: string): Record<string, unknown> {
  const candidate = value[key];
  if (!isRecord(candidate)) {
    throw new Error(`${label} did not include object field ${key}`);
  }
  return candidate;
}

function recordArray(value: Record<string, unknown>, key: string, label: string): Record<string, unknown>[] {
  const candidate = value[key];
  if (!Array.isArray(candidate) || candidate.some((item) => !isRecord(item))) {
    throw new Error(`${label} did not include object array ${key}`);
  }
  return candidate;
}

function stringField(value: Record<string, unknown>, key: string, label: string): string {
  const candidate = value[key];
  if (typeof candidate !== "string" || candidate.trim() === "") {
    throw new Error(`${label} did not include string field ${key}`);
  }
  return candidate;
}

function nullableUuidField(value: Record<string, unknown>, key: string, label: string): string | null {
  const candidate = value[key];
  if (candidate === null) return null;
  if (typeof candidate !== "string" || !UUID_PATTERN.test(candidate)) {
    throw new Error(`${label} did not include a valid nullable UUID field ${key}`);
  }
  return candidate;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
