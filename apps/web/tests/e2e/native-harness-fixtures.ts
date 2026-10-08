import { test as base, expect, type Page, type Response } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import { constants, closeSync, existsSync, fstatSync, lstatSync, openSync, readFileSync, readdirSync, realpathSync, writeFileSync, mkdirSync, chmodSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

import type { components as CP } from "../../src/shared/api/schema.js";
import type { components as Product } from "../../src/shared/api/product-schema.js";

export type ProfileRef = Product["schemas"]["ProfileRef"];
export type NativeContext = CP["schemas"]["NativeWorkerContextView"];
export type ResultOverview = CP["schemas"]["TaskResultOverviewResponse"];
export type ResultDetail = CP["schemas"]["TaskResultDetailResponse"];
export type Planning = CP["schemas"]["PlanningRequestDetailResponse"];
export type Materialization = CP["schemas"]["PlanningMaterializationResponse"];
export type NativeTrace = CP["schemas"]["NativeWorkerTraceView"];
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Ids = Record<string, Json>;

export const NATIVE_SOURCE_PATHS = [
  "apps/server/tests/support/native_product_harness.py", "apps/server/tests/support/native_product_flow.py",
  "apps/server/tests/unit/test_native_product_harness.py", "apps/server/tests/unit/test_native_product_flow.py",
  "apps/server/tests/integration/test_product_native_flow.py", "apps/web/playwright.native.config.ts",
  "apps/web/tests/e2e/native-harness-fixtures.ts", "apps/web/tests/e2e/native-harness.spec.ts",
  "apps/web/tests/e2e/native-harness-resume.spec.ts", "scripts/native-harness-check.py", "scripts/tests/test_native_harness_check.py",
  "apps/server/tests/support/agent_management_desktop.py", "apps/server/tests/unit/test_agent_management_targets.py",
  "apps/server/tests/support/controlled_knowledge.py", "apps/server/tests/unit/test_external_knowledge_fixture.py", "apps/server/tests/integration/conftest.py",
  "apps/server/src/sidera_product/agents/routes.py", "apps/server/src/sidera_product/agents/schemas.py",
  "apps/web/src/features/agents/api.ts", "apps/web/src/features/agents/ResourceEditor.tsx",
  "services/control-plane/src/sidera_control_plane/application/event_ingestor.py",
  "services/control-plane/src/sidera_control_plane/application/worker_gateway.py",
  "services/control-plane/src/sidera_control_plane/application/execution_code_inputs.py",
  "services/control-plane/src/sidera_control_plane/application/activation_reconciler.py",
  "services/control-plane/src/sidera_control_plane/application/context_requests.py",
  "services/control-plane/src/sidera_control_plane/application/handoff_context.py",
] as const;
export const NATIVE_STAGES = ["configuration", "conversation", "goal_submitted", "proposal_ready", "plan_confirmed",
  "dependency_execution", "worker_input", "result_verified", "result_accepted", "navigation_history", "owned_cleanup"] as const;
export type Stage = typeof NATIVE_STAGES[number];

export interface NativeManifest {
  schema_version: 1; owner_uid: number; allocation_id: string; workspace_id: string; namespace: string; principal_id: string;
  repository_directory: string; repository_head: string; source_root: string;
  source_pins: Record<string, { sha256: string; scope: "producer" | "ui" }>;
  config_path: string; config_hash: string; mode: "fake" | "real"; controlled_bridge: boolean;
  base_url: string; backend_url: string; controller_url: string | null; transport_path: string;
  checkpoint_path: string; cleanup_path: string; owner_receipt_path: string; guard_custody: "lab" | "caller";
  process_identities: { owner_pid: number; backend_port: number; web_port: number }; target_facts: Json[];
}
export interface Checkpoint {
  schema_version: 1; owner_uid: number; workspace_id: string; allocation_id: string; config_hash: string;
  step: Stage; step_id: string; status: "pending" | "passed" | "failed" | "incomplete" | "invalidated";
  original_ids: Ids; versions: Ids; fingerprint: string; observed: Ids; time: string; sequence: number;
}
export interface NativeFacts {
  workspace_id: string;
  functional: Ids;
  native: { opens: { run_id: string; role: string; profile_ref: ProfileRef }[];
    inputs: { run_id: string; response_id: string; text_hash: string }[];
    original_run_id: string | null; accepted_inputs: Record<string, string>;
    worker_facts: Record<string, { execution: { id: string }; worker_item: { id: string } }>;
    physical_inputs: { execution_id: string; input_hash: string; input_commit: string; input_tree: string; selection_id: string; selection_hash: string }[];
    inspections: Record<string, { process_state: string; response_state: string }> };
  knowledge: { workspace_id: string; known_empty_catalog: boolean; closed: boolean;
    calls: { method: string; path: string; workspace_id: string; status: number; item_count: number | null }[] };
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function uid(): number { return process.getuid!(); }
function uuid(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}
function privateDirectory(path: string) {
  if (!isAbsolute(path) || resolve(path) !== path || realpathSync(path) !== path) throw new Error("native_private_directory_refused");
  const info = lstatSync(path);
  if (!info.isDirectory() || info.uid !== uid() || (info.mode & 0o777) !== 0o700) throw new Error("native_private_directory_refused");
}
export function readProtectedJson(path: string): unknown {
  try {
    privateDirectory(dirname(path));
    if (resolve(path) !== path || realpathSync(path) !== path) throw new Error();
    const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const info = fstatSync(fd);
      if (!info.isFile() || info.uid !== uid() || (info.mode & 0o777) !== 0o600 || info.size > 2097152) throw new Error();
      return JSON.parse(readFileSync(fd, "utf8")) as unknown;
    } finally { closeSync(fd); }
  } catch { throw new Error("native_private_file_refused"); }
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (object(value)) return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function sha(value: string | Buffer): string { return createHash("sha256").update(value).digest("hex"); }
function noSecrets(value: unknown) {
  if (object(value)) {
    for (const [key, item] of Object.entries(value)) {
      if (/token|cookie|authorization|bootstrap|native_handle|password|secret/i.test(key)) throw new Error("native_checkpoint_secret_refused");
      noSecrets(item);
    }
  } else if (Array.isArray(value)) value.forEach(noSecrets);
}
function writeProtected(path: string, value: unknown, replaceSession = false) {
  privateDirectory(dirname(path));
  if (replaceSession && readdirSync(dirname(path)).includes("session.json")) readProtectedJson(path);
  const fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_NOFOLLOW |
    (replaceSession ? constants.O_TRUNC : constants.O_EXCL), 0o600);
  try { writeFileSync(fd, JSON.stringify(value)); } finally { closeSync(fd); }
}
function loopback(value: unknown) {
  if (typeof value !== "string") throw new Error();
  const url = new URL(value);
  if (url.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(url.hostname) || !url.port || url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error();
}

export function loadOwnedManifest(path: string, allowUiChange = false): NativeManifest {
  try {
    const raw = readProtectedJson(path);
    const fields = ["schema_version", "owner_uid", "allocation_id", "workspace_id", "namespace", "principal_id", "repository_directory", "repository_head",
      "source_root", "source_pins", "config_path", "config_hash", "mode", "controlled_bridge", "base_url", "backend_url", "controller_url",
      "transport_path", "checkpoint_path", "cleanup_path", "owner_receipt_path", "process_identities", "target_facts", "guard_custody"];
    if (!object(raw) || Object.keys(raw).sort().join() !== fields.sort().join() || raw.schema_version !== 1 || raw.owner_uid !== uid() ||
      !uuid(raw.workspace_id) || !uuid(raw.namespace) || typeof raw.allocation_id !== "string" ||
      !/^sidera-native-harness:product:n17:[0-9]{8}:[a-z0-9]+$/.test(raw.allocation_id)) throw new Error();
    const value = raw as unknown as NativeManifest;
    if (!["fake", "real"].includes(value.mode) || value.controlled_bridge !== (value.mode === "fake") || !["lab", "caller"].includes(value.guard_custody)) throw new Error();
    loopback(value.base_url); loopback(value.backend_url);
    if (value.mode === "fake") loopback(value.controller_url); else if (value.controller_url !== null) throw new Error();
    for (const key of ["repository_directory", "config_path", "transport_path", "checkpoint_path", "cleanup_path", "owner_receipt_path"] as const) {
      const selected = value[key];
      if (!isAbsolute(selected) || dirname(selected) !== dirname(path) || realpathSync(selected) !== selected) throw new Error();
    }
    for (const key of ["repository_directory", "checkpoint_path", "cleanup_path"] as const) privateDirectory(value[key]);
    const config = readProtectedJson(value.config_path), owner = readProtectedJson(value.owner_receipt_path);
    if (!object(config) || !object(config.settings) || config.settings.workspace_id !== value.workspace_id || sha(canonical(config)) !== value.config_hash ||
      !object(owner) || owner.workspace_id !== value.workspace_id || owner.config_hash !== value.config_hash || owner.owner_pid !== value.process_identities.owner_pid) throw new Error();
    if (!isAbsolute(value.source_root) || realpathSync(value.source_root) !== value.source_root || Object.keys(value.source_pins).sort().join() !== [...NATIVE_SOURCE_PATHS].sort().join()) throw new Error();
    for (const [name, pin] of Object.entries(value.source_pins)) {
      const source = join(value.source_root, name), scope = name.startsWith("apps/web/") ? "ui" : "producer";
      if (pin.scope !== scope || Object.keys(pin).sort().join() !== "scope,sha256" || !/^[0-9a-f]{64}$/.test(pin.sha256) || realpathSync(source) !== source ||
        (sha(readFileSync(source)) !== pin.sha256 && !(allowUiChange && scope === "ui"))) throw new Error();
    }
    return value;
  } catch { throw new Error("native_manifest_refused"); }
}

export function exactNativeTrace(value: unknown, pin: unknown): boolean {
  if (!object(value) || !object(pin) || !object(value.profile_ref) || !object(pin.profile_ref) || !Array.isArray(value.events)) return false;
  if (["workspace_id", "worker_item_id", "execution_id", "run_id"].some(key => !uuid(pin[key]) || value[key] !== pin[key]) ||
    canonical(value.profile_ref) !== canonical(pin.profile_ref) || value.profile_ref.workspace_id !== pin.workspace_id ||
    !Number.isSafeInteger(value.next_sequence) || typeof value.has_more !== "boolean") return false;
  let previous = 0;
  for (const event of value.events) {
    if (!object(event) || event.run_id !== pin.run_id || typeof event.sequence !== "number" || !Number.isSafeInteger(event.sequence) || event.sequence <= previous) return false;
    previous = event.sequence;
  }
  return value.events.length === 0 || previous === value.next_sequence;
}

export function currentNativeTracePin(value: unknown) {
  if (!object(value) || !object(value.execution) || !object(value.run) || !object(value.run.profile_ref) ||
      !uuid(value.workspace_id) || !uuid(value.worker_item_id) || !uuid(value.execution.execution_id) || !uuid(value.execution.run_id) ||
      value.execution.workspace_id !== value.workspace_id || value.execution.worker_item_id !== value.worker_item_id ||
      value.run.profile_ref.workspace_id !== value.workspace_id || canonical(value.execution.profile_ref) !== canonical(value.run.profile_ref)) {
    throw new Error("native_current_trace_pin_refused");
  }
  return { workspace_id: value.workspace_id, worker_item_id: value.worker_item_id,
    execution_id: value.execution.execution_id, run_id: value.execution.run_id, profile_ref: value.run.profile_ref as unknown as ProfileRef };
}

export function nativeScenario(manifest: NativeManifest): "main" | "transition" {
  const config = readProtectedJson(manifest.config_path);
  if (!object(config) || !object(config.settings) || config.settings.workspace_id !== manifest.workspace_id || sha(canonical(config)) !== manifest.config_hash) {
    throw new Error("native_scenario_refused");
  }
  const scenario = "scenario" in config ? config.scenario : "main";
  if (scenario !== "main" && scenario !== "transition") throw new Error("native_scenario_refused");
  return scenario;
}

export function nativeRecords(manifest: NativeManifest): Checkpoint[] {
  privateDirectory(manifest.checkpoint_path);
  return readdirSync(manifest.checkpoint_path).map(name => {
    if (!name.endsWith(".json")) throw new Error("native_checkpoint_refused");
    const record = readProtectedJson(join(manifest.checkpoint_path, name)) as Checkpoint;
    noSecrets(record);
    if (record.schema_version !== 1 || record.owner_uid !== uid() || record.workspace_id !== manifest.workspace_id || record.allocation_id !== manifest.allocation_id ||
      record.config_hash !== manifest.config_hash || !uuid(record.step_id) || !NATIVE_STAGES.includes(record.step) ||
      !["pending", "passed", "failed", "incomplete", "invalidated"].includes(record.status)) throw new Error("native_checkpoint_refused");
    return record;
  }).sort((a, b) => a.sequence - b.sequence);
}

export function nativeScreenshotDirectory(manifest: NativeManifest): string {
  privateDirectory(dirname(manifest.transport_path));
  const directory = join(dirname(manifest.transport_path), "screenshots");
  if (!existsSync(directory)) mkdirSync(directory, { mode: 0o700 });
  privateDirectory(directory);
  return directory;
}

function stageFingerprint(manifest: NativeManifest, stage: Stage) {
  const pins: Record<string, string> = {};
  for (const [name, pin] of Object.entries(manifest.source_pins)) {
    if (pin.scope === "producer") pins[name] = pin.sha256;
    else if (stage === "navigation_history") pins[name] = sha(readFileSync(join(manifest.source_root, name)));
  }
  return sha(canonical({ config_hash: manifest.config_hash, pins }));
}

export class NativeFixture {
  readonly manifest: NativeManifest;
  readonly page: Page;
  ids: Ids = {};
  stage: Stage = "configuration";
  stepId: string = randomUUID();
  versions: Ids = {};
  readonly mutations: { path: string; command_type: string | null; command_id: string | null }[] = [];
  readonly readOnly: boolean;
  loseInputResponse = false;
  configurationCapture: "primary" | "target" = "primary";
  private lostInput = false;
  private pending: Promise<void>[] = [];

  constructor(page: Page, manifest: NativeManifest, readOnly = false) {
    this.page = page; this.manifest = manifest; this.readOnly = readOnly;
    for (const record of nativeRecords(manifest)) Object.assign(this.ids, record.original_ids);
  }
  id(key: string): string {
    const value = this.ids[key];
    if (typeof value !== "string" || !uuid(value)) throw new Error("native_original_id_required");
    return value;
  }
  profile(): ProfileRef {
    const ref = this.ids.profile_ref;
    if (!object(ref) || ref.workspace_id !== this.manifest.workspace_id || !uuid(ref.profile_id) || typeof ref.revision !== "number" || ref.revision < 1) throw new Error("native_original_profile_required");
    return ref as unknown as ProfileRef;
  }
  path(path: string): string { return `/workspaces/${this.manifest.workspace_id}/${path}`; }
  prefix(): string { return `/api/v1/workspaces/${this.manifest.workspace_id}/`; }
  async get<T>(path: string): Promise<T> {
    if (!path.startsWith("/") || path.startsWith("//")) throw new Error("native_public_path_refused");
    const response = await this.page.request.get(this.manifest.base_url + path, { headers: { Origin: this.manifest.base_url }, timeout: 10000 });
    if (!response.ok()) throw new Error("native_original_public_read_unavailable");
    return await response.json() as T;
  }
  async readSourceContext(workerItemId: string, originalRun: string): Promise<NativeContext | null> {
    const workspace = this.manifest.workspace_id;
    const path = this.prefix() + `worker-items/${workerItemId}/native-context`;
    const current = (value: NativeContext): boolean => {
      const { context, execution, run } = value;
      return !!context && !!execution && !!run && value.workspace_id === workspace && value.worker_item_id === workerItemId &&
        value.execution_mode === "native_profile" && value.input_enabled === true && context.worker_item_id === workerItemId &&
        context.run_id === originalRun && execution.workspace_id === workspace && execution.worker_item_id === workerItemId &&
        execution.execution_id === context.execution_id && execution.run_id === originalRun &&
        run.profile_ref.workspace_id === workspace && canonical(execution.profile_ref) === canonical(run.profile_ref);
    };
    const first = await this.get<NativeContext>(path);
    if (!uuid(originalRun) || !current(first) || !first.context) return null;
    const address = first.context, pin = currentNativeTracePin(first);
    const trace = await this.get<NativeTrace>(this.prefix() + `worker-items/${workerItemId}/executions/${address.execution_id}/native-trace?after_sequence=0&limit=100`);
    const query = new URLSearchParams({ workspace_id: workspace, session_id: address.session_id, limit: "1" });
    const sessions = await this.get<CP["schemas"]["ExecutionSessionPageResponse"]>(`/api/v1/gateway/sessions?${query}`);
    const latest = await this.get<NativeContext>(path);
    if (!current(latest) || canonical(latest.context) !== canonical(address) || !exactNativeTrace(trace, pin) ||
        trace.has_more !== false || trace.next_sequence <= 0 || !trace.events.some(event => event.kind === "response.started") || sessions.items.length !== 1) return null;
    const session = sessions.items[0];
    if (!session || session.workspace_id !== workspace || session.execution_id !== address.execution_id || session.id !== address.session_id ||
        session.worker_id !== address.worker_id || session.adapter_session_id !== `agent-run:${originalRun}` || session.status !== "active" ||
        session.last_sequence < trace.next_sequence || session.version !== address.session_version) return null;
    return latest;
  }
  async waitForSourceContext(workerItemId: string): Promise<NativeContext> {
    const source: { run: string | null; context: NativeContext | null } = { run: null, context: null };
    await expect.poll(async () => {
      const facts = await this.facts();
      source.run = facts.native.original_run_id;
      return uuid(source.run) && facts.native.worker_facts.source?.worker_item.id === workerItemId;
    }, { timeout: 120_000 }).toBe(true);
    if (!source.run) throw new Error("native_original_source_run_required");
    const originalRun = source.run;
    await expect.poll(async () => {
      source.context = await this.readSourceContext(workerItemId, originalRun);
      return source.context !== null;
    }, { timeout: 120_000 }).toBe(true);
    if (!source.context) throw new Error("native_original_source_context_required");
    return source.context;
  }
  async facts(): Promise<NativeFacts> { return this.bridge("/facts") as Promise<NativeFacts>; }
  async bridge(path: "/facts" | "/release-prompt" | "/confirm-exit" | "/mark-exit-unknown", body?: Ids): Promise<unknown> {
    if (this.manifest.mode !== "fake" || !this.manifest.controlled_bridge || this.manifest.controller_url === null) throw new Error("native_controlled_bridge_disabled");
    const transport = readProtectedJson(this.manifest.transport_path);
    if (!object(transport) || transport.workspace_id !== this.manifest.workspace_id || typeof transport.controller_token !== "string") throw new Error("native_transport_refused");
    const headers = { Authorization: `Bearer ${transport.controller_token}`, "X-Sidera-Native-Workspace": this.manifest.workspace_id };
    const response = body === undefined ? await this.page.request.get(this.manifest.controller_url + path, { headers, timeout: 10000 })
      : await this.page.request.post(this.manifest.controller_url + path, { headers, data: body, timeout: 10000 });
    if (!response.ok()) throw new Error("native_original_control_refused");
    const value = await response.json() as NativeFacts;
    if (path === "/facts" && value.workspace_id !== this.manifest.workspace_id) throw new Error("native_foreign_control_facts");
    return value;
  }
  record(status: Checkpoint["status"], observed: Ids = {}, stepId = this.stepId): Checkpoint {
    const sequence = Math.max(Date.now() * 1000, (nativeRecords(this.manifest).at(-1)?.sequence ?? 0) + 1);
    const record: Checkpoint = { schema_version: 1, owner_uid: uid(), workspace_id: this.manifest.workspace_id, allocation_id: this.manifest.allocation_id,
      config_hash: this.manifest.config_hash, step: this.stage, step_id: stepId, status, original_ids: structuredClone(this.ids), versions: { ...this.versions },
      fingerprint: stageFingerprint(this.manifest, this.stage), observed, time: new Date().toISOString(), sequence };
    noSecrets(record);
    writeProtected(join(this.manifest.checkpoint_path, `${record.sequence}-${randomUUID()}.json`), record);
    return record;
  }
  recordFocusedTransition(observed: Ids): string {
    if (nativeScenario(this.manifest) !== "transition") throw new Error("native_focused_receipt_refused");
    const receipt = { workspace_id: this.manifest.workspace_id, allocation_id: this.manifest.allocation_id,
      config_hash: this.manifest.config_hash, original_ids: structuredClone(this.ids), observed, time: new Date().toISOString() };
    noSecrets(receipt);
    const path = join(dirname(this.manifest.transport_path), `focused-transition-${randomUUID()}.json`);
    writeProtected(path, receipt);
    return path;
  }
  async saveSession() {
    const state = await this.page.context().storageState();
    writeProtected(join(dirname(this.manifest.transport_path), "session.json"), state, true);
  }
  python(code: string, body: unknown): unknown {
    const result = spawnSync(join(this.manifest.source_root, "apps/server/.venv/bin/python"), ["-B", "-c", code], {
      cwd: join(this.manifest.source_root, "apps/server"), input: JSON.stringify(body), encoding: "utf8", timeout: 60000,
      env: { HOME: dirname(this.manifest.transport_path), PATH: "/usr/bin:/bin", PYTHONDONTWRITEBYTECODE: "1", LANG: "C.UTF-8" },
    });
    if (result.status !== 0 || result.error) throw new Error("native_original_read_or_repository_proof_unavailable");
    return JSON.parse(result.stdout) as unknown;
  }
  async observe(record: Checkpoint): Promise<boolean> {
    await this.saveSession();
    return this.python("import importlib.util,json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\ns=importlib.util.spec_from_file_location('native_check',Path(p['source_root'])/'scripts/native-harness-check.py')\nm=importlib.util.module_from_spec(s);s.loader.exec_module(m)\nc=m.NativeCheck(m.read_manifest(p['manifest'],allow_ui_change=True))\nprint(json.dumps(c._observe(p['record'])))", {
      source_root: this.manifest.source_root, manifest: join(dirname(this.manifest.transport_path), "manifest.json"), record,
    }) === true;
  }
  async step(stage: Stage, action: () => Promise<Ids>) {
    const selected = process.env.SIDERA_NATIVE_STAGE;
    if (selected !== undefined && selected !== stage) return;
    this.stage = stage;
    const previous = nativeRecords(this.manifest).filter(record => record.step === stage).at(-1);
    this.stepId = process.env.SIDERA_NATIVE_STEP_ID ?? previous?.step_id ?? randomUUID();
    if (!uuid(this.stepId)) throw new Error("native_step_identity_refused");
    const ownFreshIntent = previous?.status === "pending" && previous.step_id === process.env.SIDERA_NATIVE_STEP_ID &&
      previous.observed.resume === false && previous.observed.mutating_intent !== true;
    if (previous && !ownFreshIntent && !await this.observe(previous)) throw new Error("native_original_stage_incomplete");
    if (previous?.status === "passed" && previous.fingerprint === stageFingerprint(this.manifest, stage)) return;
    if (previous && !ownFreshIntent && stage !== "navigation_history") {
      if (previous.status === "passed") throw new Error("native_producer_source_changed");
      // Original public proof can reconcile a lost response, never create another intent.
      this.record("passed", { canonical: true, recovered_original: true });
      return;
    }
    this.record("pending");
    try {
      const observed = await action();
      await Promise.all(this.pending);
      await this.saveSession();
      const proof = this.record("pending", { ...observed, canonical: false });
      if (!await this.observe(proof)) throw new Error("native_canonical_stage_proof_required");
      this.record("passed", { ...observed, canonical: true });
    } catch {
      this.record("failed", { reason: "native_stage_assertion_failed" });
      throw new Error("native_stage_assertion_failed");
    }
  }
  private original(key: string, value: Json) {
    if (this.ids[key] !== undefined && canonical(this.ids[key]) !== canonical(value)) throw new Error("native_replacement_configuration_refused");
    this.ids[key] = structuredClone(value);
  }
  private configurationIntent(path: string, body: Record<string, unknown>) {
    const root = `/agents/v1/workspaces/${this.manifest.workspace_id}`;
    const prefix = this.configurationCapture === "target" ? "target_" : "";
    if (path === root + "/resources") {
      if (prefix || !uuid(body.operationId) || !object(body.draft) || body.draft.kind !== "mcp" ||
        canonical(body.draft.settings) !== canonical({ connection_id: this.id("task_tool_connection_id") }) || canonical(body.draft.secret_refs) !== "[]") {
        throw new Error("native_resource_intent_refused");
      }
      this.original("task_tool_resource_operation_id", body.operationId);
    } else if (path === root + "/profiles") {
      if (!uuid(body.operationId) || !object(body.draft) || canonical(body.draft.resource_refs) !== "[]" ||
        canonical(body.draft.runtime_ref) !== canonical({ workspace_id: this.manifest.workspace_id, runtime_id: this.id("runtime_id") })) {
        throw new Error("native_profile_create_intent_refused");
      }
      this.original(`${prefix}profile_create_operation_id`, body.operationId);
    } else if (path.startsWith(root + "/profiles/") && path.endsWith("/revisions")) {
      const created = this.ids[`${prefix}profile_create_ref`], resource = this.ids.task_tool_resource_ref;
      if (!uuid(body.operationId) || !object(created) || created.revision !== 1 || !object(resource) || resource.revision !== 1 ||
        path !== root + `/profiles/${String(created.profile_id)}/revisions` || canonical(body.expected_ref) !== canonical(created) ||
        !object(body.draft) || canonical(body.draft.resource_refs) !== canonical([resource]) ||
        canonical(body.draft.runtime_ref) !== canonical({ workspace_id: this.manifest.workspace_id, runtime_id: this.id("runtime_id") })) {
        throw new Error("native_profile_bind_intent_refused");
      }
      this.original(`${prefix}configuration_operation_id`, body.operationId);
      this.original(`${prefix}configuration_expected_ref`, created as Json);
      this.original(`${prefix}configuration_resource_ref`, resource as Json);
    }
  }
  async installObservation() {
    await this.page.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== this.manifest.base_url || !["POST", "PUT", "DELETE"].includes(request.method()) || url.pathname === "/product/v1/session/bootstrap") {
        await route.continue(); return;
      }
      const body: unknown = request.postDataJSON();
      if (!object(body)) throw new Error("native_mutation_body_refused");
      const commandType = typeof body.command_type === "string" ? body.command_type : null;
      const commandId = typeof body.command_id === "string" ? body.command_id : null;
      this.mutations.push({ path: url.pathname, command_type: commandType, command_id: commandId });
      if (this.readOnly) { await route.abort("blockedbyclient"); return; }
      if (commandId && commandType) {
        const key = { ConfirmPlanningProposal: "confirmation_command_id", SendExecutionInput: "input_command_id", CompleteTask: "acceptance_command_id",
          PrepareProfileHandoff: "handoff_prepare_command_id", ConfirmProfileHandoff: "handoff_confirm_command_id" }[commandType];
        if (key) {
          if (this.ids[key] && this.ids[key] !== commandId) throw new Error("native_replacement_command_refused");
          this.ids[key] = commandId;
        }
        if (typeof body.expected_version === "number") this.versions[commandType] = body.expected_version;
      }
      if (typeof body.operationId === "string") {
        if (url.pathname.endsWith("/turns")) this.ids.turn_operation_id = body.operationId;
        else this.configurationIntent(url.pathname, body);
      }
      this.record("pending", { payload_fingerprint: sha(canonical(body)), mutating_intent: true });
      if (commandType === "SendExecutionInput" && this.loseInputResponse && !this.lostInput) {
        this.lostInput = true;
        const response = await route.fetch();
        await this.capture(request.url(), response.status(), await response.json());
        await route.abort("failed");
      } else await route.continue();
    });
    this.page.on("response", (response: Response) => {
      const url = new URL(response.url());
      if (url.origin !== this.manifest.base_url || !["POST", "PUT"].includes(response.request().method()) || url.pathname === "/product/v1/session/bootstrap") return;
      const pending = response.json().then(value => this.capture(response.url(), response.status(), value));
      this.pending.push(pending);
      void pending.catch(() => undefined);
    });
  }
  async capture(urlText: string, status: number, value: unknown) {
    if (status < 200 || status >= 300 || !object(value)) return;
    const path = new URL(urlText).pathname;
    const root = `/agents/v1/workspaces/${this.manifest.workspace_id}`;
    if (path.endsWith("/runtimes") && object(value.ref) && uuid(value.ref.runtime_id) && value.ref.workspace_id === this.manifest.workspace_id) this.ids.runtime_id = value.ref.runtime_id;
    if (path === root + "/resources" && ["succeeded", "reconciled"].includes(String(value.state))) {
      const ref = value.result_ref;
      if (!object(ref) || Object.keys(ref).sort().join() !== "resource_id,revision,workspace_id" || !uuid(ref.resource_id) ||
        ref.workspace_id !== this.manifest.workspace_id || ref.revision !== 1 || canonical(ref) !== canonical(value.target_ref) ||
        value.operationId !== this.ids.task_tool_resource_operation_id) throw new Error("native_resource_capture_refused");
      this.original("task_tool_resource_ref", ref as Json);
    }
    const create = path === root + "/profiles";
    const bind = path.startsWith(root + "/profiles/") && path.endsWith("/revisions");
    if ((create || bind) && ["succeeded", "reconciled"].includes(String(value.state))) {
      const operationKey = create ? "profile_create_operation_id" : "configuration_operation_id";
      const prefixes = ["", "target_"].filter(prefix => this.ids[`${prefix}${operationKey}`] === value.operationId);
      if (prefixes.length !== 1) throw new Error("native_configuration_capture_refused");
      const prefix = prefixes[0];
      const ref = value.result_ref;
      if (!object(ref) || Object.keys(ref).sort().join() !== "profile_id,revision,workspace_id" || !uuid(ref.profile_id) ||
        ref.workspace_id !== this.manifest.workspace_id || ref.revision !== (create ? 1 : 2) || canonical(ref) !== canonical(value.target_ref) ||
        (prefix === "target_" && ref.profile_id === this.profile().profile_id)) {
        throw new Error("native_configuration_capture_refused");
      }
      if (create) {
        this.original(`${prefix}profile_create_ref`, ref as Json);
        this.original(`${prefix}profile_id`, ref.profile_id);
        if (this.ids[`${prefix}profile_ref`] === undefined) this.ids[`${prefix}profile_ref`] = ref as Json;
      } else {
        const created = this.ids[`${prefix}profile_create_ref`], current = this.ids[`${prefix}profile_ref`];
        if (!object(created) || created.revision !== 1 || created.profile_id !== ref.profile_id ||
          path !== root + `/profiles/${ref.profile_id}/revisions` ||
          canonical(this.ids[`${prefix}configuration_expected_ref`]) !== canonical(created) ||
          !object(this.ids.task_tool_resource_ref) || canonical(this.ids[`${prefix}configuration_resource_ref`]) !== canonical(this.ids.task_tool_resource_ref) ||
          (canonical(current) !== canonical(created) && canonical(current) !== canonical(ref))) throw new Error("native_configuration_capture_refused");
        this.ids[`${prefix}profile_ref`] = ref as Json;
      }
    }
    if (path === "/main-agent/v1/threads" && uuid(value.id)) this.ids.thread_id = value.id;
    if (path.endsWith("/turns") && uuid(value.id) && value.threadId === this.ids.thread_id) {
      this.ids.turn_id = value.id;
      if (uuid(value.runId)) this.ids.chat_run_id = value.runId;
    }
    if (path === "/api/v1/commands" && uuid(value.aggregate_id)) {
      if (value.command_id === this.ids.input_command_id) this.ids.input_id = value.aggregate_id;
      if (value.command_id === this.ids.handoff_prepare_command_id) this.ids.handoff_proposal_id = value.aggregate_id;
      if (value.command_id === this.ids.handoff_confirm_command_id) this.ids.handoff_transition_id = value.aggregate_id;
    }
    this.record("pending", { original_response_observed: true });
  }
  async authorize() {
    await this.page.goto(this.path("runtimes"));
    const sessionResponse = await this.page.request.get(this.manifest.base_url + "/product/v1/session");
    if (!sessionResponse.ok()) {
      if (this.readOnly || nativeRecords(this.manifest).some(record => record.step === "configuration" && record.status === "passed")) throw new Error("native_current_human_required");
      const transport = readProtectedJson(this.manifest.transport_path);
      if (!object(transport) || transport.workspace_id !== this.manifest.workspace_id || typeof transport.bootstrap_file !== "string" || dirname(transport.bootstrap_file) !== join(dirname(this.manifest.transport_path), "bootstrap")) throw new Error("native_transport_refused");
      const bootstrap = readProtectedJson(transport.bootstrap_file);
      if (!object(bootstrap) || typeof bootstrap.token !== "string") throw new Error("native_transport_refused");
      await this.page.getByRole("button", { name: "本地会话", exact: true }).click();
      const dialog = this.page.getByRole("dialog", { name: "本地会话" });
      await dialog.getByLabel("CLI 令牌").fill(bootstrap.token);
      await dialog.getByRole("button", { name: "连接", exact: true }).click();
      await expect(dialog.getByText(this.manifest.principal_id, { exact: true })).toBeVisible();
      await dialog.getByRole("button", { name: "关闭本地会话" }).click();
    }
    const current = await this.get<Product["schemas"]["LocalSessionView"]>("/product/v1/session");
    expect(current.workspace_id).toBe(this.manifest.workspace_id); expect(current.principal_id).toBe(this.manifest.principal_id);
    await this.saveSession();
  }
  repositoryProof(candidate: ResultDetail["candidate"], checks: ResultDetail["verifications"]): Json {
    return this.python("import json,sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\nfrom tests.support.native_product_flow import verify_result_repository\np=json.load(sys.stdin)\nlab=SimpleNamespace(repository=Path(p['repository']),manifest={'repository_head':p['original_head']})\nprint(json.dumps(verify_result_repository(lab,p['candidate'],p['checks'])))", {
      repository: this.manifest.repository_directory, original_head: this.manifest.repository_head, candidate, checks,
    }) as Json;
  }
  async screenshot(name: string) {
    await expect(this.page.locator('input[type="password"]')).toHaveCount(0);
    const directory = nativeScreenshotDirectory(this.manifest);
    const viewport = this.page.viewportSize();
    const path = join(directory, `${name}-${viewport?.width}x${viewport?.height}-${randomUUID()}.png`);
    const buffer = await this.page.screenshot({ path, fullPage: false });
    chmodSync(path, 0o600);
    const surface = await this.page.locator(".main-surface").boundingBox();
    const tabs = await this.page.getByRole("navigation", { name: "已打开的任务" }).boundingBox();
    if (!viewport || !surface || !tabs) throw new Error("native_view_dimensions_missing");
    expect(surface.width).toBeGreaterThan(400); expect(surface.height).toBeGreaterThan(200);
    expect(tabs.x).toBeGreaterThanOrEqual(surface.x - 1); expect(tabs.x + tabs.width).toBeLessThanOrEqual(surface.x + surface.width + 1);
    return { path, width: viewport.width, height: viewport.height, sha256: sha(buffer) };
  }
}

export const test = base.extend<{ native: NativeFixture }>({
  native: async ({ page }, runFixture, info) => {
    const path = process.env.SIDERA_NATIVE_MANIFEST;
    if (!path) throw new Error("native_owned_manifest_required");
    const manifest = loadOwnedManifest(path, process.env.SIDERA_NATIVE_RESUME === "1");
    if (manifest.mode !== "fake") throw new Error("native_provider_mode_not_authorized");
    const fixture = new NativeFixture(page, manifest, process.env.SIDERA_NATIVE_READ_ONLY === "1" || info.file.endsWith("native-harness-resume.spec.ts"));
    await fixture.installObservation();
    try { await runFixture(fixture); } finally { await fixture.saveSession(); }
  },
});
export { expect };

export async function assertNativeNavigation(native: NativeFixture): Promise<Ids> {
  const { page } = native, root = native.id("root_task_id"), source = native.id("source_worker_item_id");
  const before = native.mutations.length;
  const screenshots: Json[] = [];
  const tabs = page.getByRole("navigation", { name: "已打开的任务" });
  await page.goto(native.path(`tasks/${root}`));
  await expect(tabs.locator(".workspace-tab.is-active").locator("a")).toHaveAttribute("href", native.path(`tasks/${root}`));
  const originalSourceClose = tabs.getByRole("button", { name: "关闭source", exact: true });
  if (await originalSourceClose.count() > 0) {
    await originalSourceClose.click();
    await expect(originalSourceClose).toHaveCount(0);
  }
  await page.getByRole("tab", { name: "看板", exact: true }).click();
  await expect(page.getByRole("region", { name: "子任务状态看板" })).toBeVisible();
  screenshots.push(await native.screenshot("board"));
  await page.getByRole("tab", { name: "DAG", exact: true }).click();
  await expect(page.getByRole("region", { name: "任务 DAG" })).toBeVisible();
  screenshots.push(await native.screenshot("dag"));
  await page.getByRole("tab", { name: "看板", exact: true }).click();
  await page.getByRole("region", { name: "子任务状态看板", exact: true })
    .getByRole("button", { name: /^打开子任务：/ }).click();
  await expect(page).toHaveURL(native.manifest.base_url + native.path(`tasks/${root}/subtasks/subtask-one`));
  const subtaskUrl = page.url();
  await page.getByRole("button", { name: "打开 Worker：source", exact: true }).click();
  await expect(page).toHaveURL(native.manifest.base_url + native.path(`worker-items/${source}`));
  await expect(tabs.locator(".workspace-tab.is-active").locator("a")).toHaveAttribute("href", native.path(`worker-items/${source}`));
  await tabs.locator(".workspace-tab.is-active").getByRole("button", { name: /^关闭/ }).click();
  await expect(page).toHaveURL(subtaskUrl);
  await page.getByRole("button", { name: "打开 Worker：source", exact: true }).click();
  await page.reload();
  await expect(page.getByRole("heading", { name: "工作记录", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Agent Trace" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled();
  const context = await native.get<NativeContext>(native.prefix() + `worker-items/${source}/native-context`);
  const pin = currentNativeTracePin(context);
  expect(pin.run_id).toBe(native.id("source_run_id")); expect(pin.execution_id).toBe(native.id("source_execution_id"));
  expect(context.input_enabled).toBe(false); expect(pin.profile_ref).toEqual(native.profile());
  screenshots.push(await native.screenshot("worker-item"));
  await page.getByRole("button", { name: "查看当前 Agent Trace", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Agent Trace" });
  await dialog.getByText("原始执行", { exact: true }).click();
  await expect(dialog.getByText(pin.run_id, { exact: true })).toBeVisible();
  await expect(dialog.getByText(pin.execution_id, { exact: true })).toBeVisible();
  await expect(dialog.getByRole("region", { name: "原始 Run 记录" })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
  const tracePath = native.prefix() + `worker-items/${source}/executions/${pin.execution_id}/native-trace`;
  const first = await native.get<NativeTrace>(tracePath + "?after_sequence=0&limit=1");
  expect(exactNativeTrace(first, pin)).toBe(true); expect(first.events.length).toBe(1);
  const next = await native.get<NativeTrace>(tracePath + `?after_sequence=${first.next_sequence}&limit=100`);
  expect(exactNativeTrace(next, pin)).toBe(true);
  expect(next.events.every(event => event.sequence > first.next_sequence)).toBe(true);
  await page.goto(native.path(`tasks/${root}/executions/${pin.execution_id}/sessions/${native.id("source_session_id")}/trace`));
  await expect(page.getByRole("region", { name: "Worker Trace 页面" })).toBeVisible();
  await page.getByText("原始执行", { exact: true }).click();
  await expect(page.getByText(pin.run_id, { exact: true })).toBeVisible();
  await expect(page.getByText(pin.profile_ref.profile_id, { exact: true })).toBeVisible();
  screenshots.push(await native.screenshot("history-trace"));
  await page.goto(native.path(`tasks/${root}`));
  await page.getByRole("tab", { name: "成果", exact: true }).click();
  await expect(page.getByRole("region", { name: "成果验收" })).toBeVisible();
  await expect(page.getByRole("button", { name: "接受成果", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "查询原始验收", exact: true }).click();
  screenshots.push(await native.screenshot("result"));
  const overview = await native.get<ResultOverview>(native.prefix() + `tasks/${root}/results`);
  expect(overview.task_status).toBe("succeeded"); expect(overview.acceptance?.command_id).toBe(native.id("acceptance_command_id"));
  await page.goto(native.path("tasks"));
  const related = page.locator(`a.task-row[href="${native.path(`tasks/${root}`)}"]`);
  await expect(related).toBeVisible();
  await related.click(); await expect(page).toHaveURL(native.manifest.base_url + native.path(`tasks/${root}`));
  await page.goto(native.path("inbox"));
  await page.getByRole("tab", { name: /^进度通报/ }).click();
  const threads = await native.get<CP["schemas"]["NotificationThreadPageResponse"]>(native.prefix() + "notification-threads?category=progress&limit=100");
  const thread = threads.items.find(item => item.root_task_id === root && item.deep_link !== null);
  expect(thread, "Actual related notification is required").toBeDefined();
  if (!thread) throw new Error("native_related_notification_required");
  const threadToggle = page.locator(`button[aria-controls="notification-thread-history-${thread.id}"]`);
  await expect(threadToggle).toHaveCount(1);
  await expect(threadToggle).toHaveAccessibleName(`展开${thread.title}`);
  await threadToggle.click();
  const relatedHistory = page.locator(`#notification-thread-history-${thread.id}`).getByRole("region", { name: `${thread.title}的线程历史` });
  await relatedHistory.getByRole("button", { name: thread.worker_item_id ? "打开 WorkerItem" : thread.subtask_id ? "打开子任务" : "打开任务", exact: true }).click();
  const expectedTarget = new URL(thread.deep_link!, native.manifest.base_url);
  await expect(page).toHaveURL(expectedTarget.href);
  await page.goto(native.path("knowledge"));
  await expect(page.getByText("没有匹配的知识页", { exact: true })).toBeVisible();
  await page.getByRole("searchbox", { name: "搜索知识" }).fill("native owned empty");
  await expect(page.getByText("没有匹配的知识页", { exact: true })).toBeVisible();
  await expect.poll(async () => (await native.facts()).knowledge.calls.filter(call => call.path.endsWith("/search") && call.status === 200).length).toBeGreaterThan(0);
  const knowledge = (await native.facts()).knowledge;
  expect(knowledge.workspace_id).toBe(native.manifest.workspace_id); expect(knowledge.known_empty_catalog).toBe(true);
  expect(knowledge.calls.some(call => call.path.endsWith("/pages") && call.status === 200)).toBe(true);
  for (const target of [`tasks/${root}`, `worker-items/${source}`]) {
    await page.setViewportSize({ width: 1366, height: 768 });
    await page.goto(native.path(target));
    await expect(page.getByRole("navigation", { name: "已打开的任务" })).toBeVisible();
    screenshots.push(await native.screenshot(target.startsWith("tasks/") ? "task-tabs" : "worker-tabs"));
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  expect(native.mutations.slice(before)).toEqual([]);
  return { screenshots, original_run_id: pin.run_id, original_execution_id: pin.execution_id,
    ordinary_task_navigation: true,
    original_trace_cursor: next.next_sequence, related_notification_id: thread.id,
    controlled_knowledge: { known_empty_catalog: true, page_list_calls: knowledge.calls.filter(call => call.path.endsWith("/pages")).length,
      search_calls: knowledge.calls.filter(call => call.path.endsWith("/search")).length } };
}
