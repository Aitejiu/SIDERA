import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { relative, resolve } from "node:path";

import type {
  Reporter,
  TestCase,
  TestResult,
} from "@playwright/test/reporter";

type ActionStatus = "NOT_RUN" | "PASS" | "FAIL" | "INCOMPLETE";
type ActionEvidence = {
  id: string;
  status: ActionStatus;
  evidence_kind: "simulated_interview";
  test_id: string;
  evidence_paths: string[];
  failure_code: string | null;
  batch_id: string;
  project: string;
  viewport: { width: number; height: number } | null;
  stable_test_key: string;
};

type RunReport = {
  runId: string;
  runRoot: string;
  scenario: string;
  codeCommit: string;
  implementationFingerprint: string;
  fingerprintVersion: number;
  batchId: string;
  catalogFingerprint: string;
  observed: Map<string, ActionEvidence[]>;
  observedChecks: Map<string, ActionEvidence[]>;
};

const catalogPath = resolve(import.meta.dirname, "action-catalog.json");
const overallCheckIds = ["M1", "M2", "M3", "M4", "M5", "M6"];

export default class InterviewCoverageReporter implements Reporter {
  private readonly runs = new Map<string, RunReport>();
  private readonly catalogIds: Set<string>;
  private readonly catalogFingerprint: string;
  private readonly batchId: string;
  private hasUnknownAnnotation = false;

  constructor() {
    this.catalogIds = new Set(readCatalogIds());
    this.catalogFingerprint = createHash("sha256").update(readFileSync(catalogPath)).digest("hex");
    this.batchId = process.env.SIDERA_INTERVIEW_BATCH_ID ?? cryptoRandomUuid();
  }

  printsToStdio(): boolean {
    return false;
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const annotations = [...test.annotations, ...result.annotations];
    const manifestPath = annotationValue(annotations, "interview-run");
    if (manifestPath === null) return;
    const manifest = readManifest(manifestPath);
    if (manifest === null) return;
    const run = this.runs.get(manifest.runId) ?? {
      runId: manifest.runId,
      runRoot: manifest.runRoot,
      scenario: manifest.scenario,
      codeCommit: manifest.codeCommit,
      implementationFingerprint: manifest.implementationFingerprint,
      fingerprintVersion: manifest.fingerprintVersion,
      batchId: this.batchId,
      catalogFingerprint: this.catalogFingerprint,
      observed: new Map<string, ActionEvidence[]>(),
      observedChecks: new Map<string, ActionEvidence[]>(),
    };
    this.runs.set(manifest.runId, run);

    const actionIds = [...new Set(
      annotations
        .filter((annotation) => annotation.type === "action")
        .map((annotation) => annotation.description?.trim() ?? "")
        .filter(Boolean),
    )];
    const unknown = actionIds.filter((id) => !this.catalogIds.has(id));
    if (unknown.length > 0) {
      this.hasUnknownAnnotation = true;
      console.error(`interview action annotation is not in catalog: ${unknown.join(",")}`);
    }
    const knownActionIds = actionIds.filter((id) => this.catalogIds.has(id));
    const checkIds = [...new Set(
      annotations
        .filter((annotation) => annotation.type === "check")
        .map((annotation) => annotation.description?.trim() ?? "")
        .filter(Boolean),
    )];
    const unknownChecks = checkIds.filter((id) => !overallCheckIds.includes(id));
    if (unknownChecks.length > 0) {
      this.hasUnknownAnnotation = true;
      console.error(`interview overall check annotation is invalid: ${unknownChecks.join(",")}`);
    }
    const knownCheckIds = checkIds.filter((id) => overallCheckIds.includes(id));
    if (knownActionIds.length === 0 && knownCheckIds.length === 0) return;

    const status = result.status === "passed"
      ? "PASS"
      : result.status === "skipped"
        ? "INCOMPLETE"
        : "FAIL";
    const evidencePath = writeTestEvidence(run, test, result, knownActionIds, knownCheckIds, status);
    const project = test.parent.project()?.name ?? "none";
    const viewport = projectViewport(project);
    const stableTestKey = stableTestIdentity(test);
    const evidence: ActionEvidence = {
      id: "",
      status,
      evidence_kind: "simulated_interview",
      test_id: test.id,
      evidence_paths: [evidencePath],
      failure_code: status === "FAIL" ? failureCode(result) : status === "INCOMPLETE" ? "test_skipped" : null,
      batch_id: run.batchId,
      project,
      viewport,
      stable_test_key: stableTestKey,
    };
    for (const actionId of knownActionIds) {
      run.observed.set(actionId, [
        ...(run.observed.get(actionId) ?? []),
        { ...evidence, id: actionId },
      ]);
    }
    for (const checkId of knownCheckIds) {
      run.observedChecks.set(checkId, [
        ...(run.observedChecks.get(checkId) ?? []),
        { ...evidence, id: checkId },
      ]);
    }
  }

  onEnd(): void {
    for (const run of this.runs.values()) {
      writeActionReport(run, [...this.catalogIds]);
      writeOverallReport(run);
      registerChildRun(run, process.env.SIDERA_INTERVIEW_DEMO_PARENT_MANIFEST);
    }
    if (this.hasUnknownAnnotation) process.exitCode = 1;
  }
}

function readCatalogIds(): string[] {
  try {
    const value: unknown = JSON.parse(readFileSync(catalogPath, "utf8"));
    if (!isRecord(value) || !Array.isArray(value.actions)) return [];
    return value.actions
      .map((item) => isRecord(item) && typeof item.id === "string" ? item.id : null)
      .filter((id): id is string => id !== null);
  } catch {
    return [];
  }
}

function annotationValue(
  annotations: readonly { type: string; description?: string }[],
  type: string,
): string | null {
  const value = annotations.find((annotation) => annotation.type === type)?.description?.trim();
  return value === undefined || value === "" ? null : value;
}

function readManifest(path: string): {
  runId: string;
  runRoot: string;
  scenario: string;
  codeCommit: string;
  implementationFingerprint: string;
  fingerprintVersion: number;
} | null {
  try {
    const value: unknown = JSON.parse(readFileSync(path, "utf8"));
    if (!isRecord(value)
      || typeof value.run_id !== "string"
      || typeof value.run_root !== "string"
      || typeof value.scenario !== "string"
      || typeof value.code_commit !== "string"
      || typeof value.implementation_fingerprint !== "string"
      || typeof value.fingerprint_version !== "number") return null;
    return {
      runId: value.run_id,
      runRoot: value.run_root,
      scenario: value.scenario,
      codeCommit: value.code_commit,
      implementationFingerprint: value.implementation_fingerprint,
      fingerprintVersion: value.fingerprint_version,
    };
  } catch {
    return null;
  }
}

function writeTestEvidence(
  run: RunReport,
  test: TestCase,
  result: TestResult,
  actionIds: readonly string[],
  checkIds: readonly string[],
  status: ActionStatus,
): string {
  const evidenceRoot = resolve(run.runRoot, "reports", "browser");
  mkdirSync(evidenceRoot, { recursive: true, mode: 0o700 });
  chmodSync(evidenceRoot, 0o700);
  const key = createHash("sha256").update(`${test.id}:${result.retry}`).digest("hex").slice(0, 20);
  const path = resolve(evidenceRoot, `${key}.json`);
  const payload = {
    schema_version: 2,
    run_id: run.runId,
    batch_id: run.batchId,
    test_id: test.id,
    title_path: test.titlePath(),
    status,
    action_ids: actionIds,
    check_ids: checkIds,
    retry: result.retry,
    suite: "browser",
    project: test.parent.project()?.name ?? "none",
    viewport: projectViewport(test.parent.project()?.name ?? "none"),
    stable_test_key: stableTestIdentity(test),
    implementation_fingerprint: run.implementationFingerprint,
    fingerprint_version: run.fingerprintVersion,
    duration_ms: result.duration,
    failure: result.error?.message ?? null,
    attachments: result.attachments
      .map((attachment) => attachment.path)
      .filter((value): value is string => typeof value === "string"),
  };
  writeFileSync(path, `${JSON.stringify(payload, null, 2)}\n`, { mode: 0o600 });
  chmodSync(path, 0o600);
  return path;
}

function writeActionReport(run: RunReport, catalogIds: readonly string[]): void {
  const actions = catalogIds.map((id) => {
    const observations = run.observed.get(id) ?? [];
    const selected = observations.find((item) => item.status === "FAIL")
      ?? observations.find((item) => item.status === "INCOMPLETE")
      ?? observations[0];
    return selected ?? {
      id,
      status: "NOT_RUN" as const,
      evidence_kind: "simulated_interview" as const,
      test_id: "",
      evidence_paths: [],
      failure_code: null,
      batch_id: run.batchId,
      project: "none",
      viewport: null,
      stable_test_key: "",
    };
  });
  const reportRoot = resolve(run.runRoot, "reports");
  mkdirSync(reportRoot, { recursive: true, mode: 0o700 });
  chmodSync(reportRoot, 0o700);
  const path = resolve(reportRoot, "action-report.json");
  writeFileSync(path, `${JSON.stringify({
    schema_version: 2,
    run_id: run.runId,
    batch_id: run.batchId,
    scenario: run.scenario,
    code_commit: run.codeCommit,
    implementation_fingerprint: run.implementationFingerprint,
    fingerprint_version: run.fingerprintVersion,
    catalog_fingerprint: run.catalogFingerprint,
    actions,
  }, null, 2)}\n`, { mode: 0o600 });
  chmodSync(path, 0o600);
}

function writeOverallReport(run: RunReport): void {
  const checks = overallCheckIds.map((id) => {
    const observations = run.observedChecks.get(id) ?? [];
    const selected = observations.find((item) => item.status === "FAIL")
      ?? observations.find((item) => item.status === "INCOMPLETE")
      ?? observations[0];
    return selected ?? {
      id,
      status: "NOT_RUN" as const,
      evidence_kind: "simulated_interview" as const,
      test_id: "",
      evidence_paths: [],
      failure_code: null,
      batch_id: run.batchId,
      project: "none",
      viewport: null,
      stable_test_key: "",
    };
  });
  const reportRoot = resolve(run.runRoot, "reports");
  mkdirSync(reportRoot, { recursive: true, mode: 0o700 });
  chmodSync(reportRoot, 0o700);
  const path = resolve(reportRoot, "overall-checks.json");
  writeFileSync(path, `${JSON.stringify({
    schema_version: 2,
    run_id: run.runId,
    batch_id: run.batchId,
    scenario: run.scenario,
    code_commit: run.codeCommit,
    implementation_fingerprint: run.implementationFingerprint,
    fingerprint_version: run.fingerprintVersion,
    checks,
  }, null, 2)}\n`, { mode: 0o600 });
  chmodSync(path, 0o600);
}

function registerChildRun(run: RunReport, parentManifestPath: string | undefined): void {
  if (parentManifestPath === undefined || parentManifestPath.trim() === "") return;
  try {
    const parentValue: unknown = JSON.parse(readFileSync(parentManifestPath, "utf8"));
    if (!isRecord(parentValue)
      || typeof parentValue.run_id !== "string"
      || typeof parentValue.run_root !== "string") {
      throw new Error("parent_manifest_invalid");
    }
    const expectedRoot = resolve(parentValue.run_root, "..", run.runId);
    if (resolve(run.runRoot) !== expectedRoot) throw new Error("child_run_root_mismatch");
    const path = resolve(parentValue.run_root, "reports", "child-runs.json");
    mkdirSync(resolve(parentValue.run_root, "reports"), { recursive: true, mode: 0o700 });
    let registry: { schema_version: number; run_id: string; children: Array<Record<string, unknown>> };
    try {
      const value: unknown = JSON.parse(readFileSync(path, "utf8"));
      if (!isRecord(value)
        || value.schema_version !== 1
        || value.run_id !== parentValue.run_id
        || !Array.isArray(value.children)) throw new Error("registry_invalid");
      registry = {
        schema_version: 1,
        run_id: parentValue.run_id,
        children: value.children.filter(isRecord),
      };
    } catch (error) {
      if (error instanceof Error && error.message === "registry_invalid") throw error;
      registry = { schema_version: 1, run_id: parentValue.run_id, children: [] };
    }
    registry.children = registry.children.filter((child) => child.run_id !== run.runId);
    registry.children.push({
      run_id: run.runId,
      run_root: run.runRoot,
      suite: "browser",
      status: hasFailure(run) ? "failed" : "passed",
    });
    registry.children.sort((left, right) => String(left.run_id).localeCompare(String(right.run_id)));
    writeFileSync(path, `${JSON.stringify(registry, null, 2)}\n`, { mode: 0o600 });
    chmodSync(path, 0o600);
  } catch (error) {
    process.exitCode = 1;
    console.error(`interview child run registration failed: ${error instanceof Error ? error.message : "unknown"}`);
  }
}

function hasFailure(run: RunReport): boolean {
  return [...run.observed.values(), ...run.observedChecks.values()]
    .some((observations) => observations.some((item) => item.status === "FAIL"));
}

function failureCode(result: TestResult): string {
  if (result.status === "timedOut") return "test_timeout";
  if (result.status === "interrupted") return "test_interrupted";
  return result.error?.message?.split("\n", 1)[0]?.slice(0, 200) ?? "test_failed";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function cryptoRandomUuid(): string {
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

function stableTestIdentity(test: TestCase): string {
  const file = relative(process.cwd(), test.location.file).replaceAll("\\", "/");
  return file + "::" + test.titlePath().join(" > ");
}

function projectViewport(project: string): { width: number; height: number } | null {
  switch (project) {
    case "desktop":
      return { width: 1440, height: 900 };
    case "tablet":
      return { width: 1024, height: 768 };
    case "mobile":
      return { width: 390, height: 844 };
    default:
      return null;
  }
}
