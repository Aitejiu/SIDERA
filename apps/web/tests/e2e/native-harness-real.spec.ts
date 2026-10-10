import { realTest, expect, loadOwnedManifest, loadRealConfig, realScenario, realFocusedRecords, realNavigationRecovery,
  readRealNativeTrace, currentNativeTracePin, type NativeFixture, type NativeContext, type NativeTrace,
  type Planning, type Materialization, type ResultOverview, type ResultDetail, type ProfileRef,
  type Json, type Ids, type RealCase, type Stage } from "./native-harness-fixtures.js";
import type { components as CP } from "../../src/shared/api/schema.js";
import type { components as Product } from "../../src/shared/api/product-schema.js";

type Proposal = CP["schemas"]["PlanningProposalResponse"];
type WorkerView = CP["schemas"]["WorkerWorkspaceProjectionResponse"];
type HandoffProposal = CP["schemas"]["NativeHandoffProposalResponse"];
type HandoffTransition = CP["schemas"]["NativeHandoffTransitionResponse"];
type Setup = { workspace_id: string; version: number; repository_ref: string;
  planning_policy_ref: { policy_id: string; version: number }; planner_agent_ref: ProfileRef };
type ShapeItem = { key: string; title: string; subtask_key?: string };
type TracePin = ReturnType<typeof currentNativeTracePin>;
type WorkerObservation = { pin: TracePin; session_id: string | null; context: NativeContext;
  started_at: string | null; finished_at: string | null; status: string; trace: NativeTrace };

const ROLE_INSTRUCTIONS = "Follow the supplied role and context. Perform only the authorized scope. " +
  "Use SIDERA task tools for Main task requests; never claim a task exists without its verified tool receipt. " +
  "Preserve repository contract, seed verifier and inputs. Use Python standard library only, without network dependencies. " +
  "Report concise steps and actual outputs. Follow explicit confirmation and current execution authority.";
const MAIN_GOAL = `Create an actual Task using SIDERA task tools for a standard-library Python JSON configuration CLI.
Wait for explicit human confirmation before any Worker execution. Use schema 6 and exactly four stable WorkerItem keys:
contract -> (cli || tests) -> integration. CLI and tests must run in parallel after contract completes;
integration must wait for both. Use hard dependencies and all-required input groups selecting immutable code results:
cli consumes contract, tests consumes contract, integration consumes cli and tests.
contract writes config_contract.json with exit_codes OK=0, INVALID_CONFIGURATION=2, INVALID_JSON=3, INPUT_UNAVAILABLE=4.
cli writes config_cli.py; tests writes test_config_cli.py; integration writes integration_report.md and executes the produced unittest suite and the unchanged seed verify_demo.py.
Only these four output files may be written; preserve README.md, verify_demo.py and inputs byte-identically.
CLI: python config_cli.py <input.json>. Input must be an object, name a string where name.strip() must be nonempty (reject empty and whitespace-only names),
port a nonboolean integer 1..65535, enabled an optional boolean defaulting to true. Valid input exits 0 with stdout JSON {"ok":true,"config":{"name":<name>,"port":<port>,"enabled":<boolean>}};
preserve the original valid name unchanged, including surrounding whitespace.
Invalid fields/types/root exit 2 with {"ok":false,"error":"INVALID_CONFIGURATION"}; malformed JSON exits 3 with INVALID_JSON;
missing/unreadable path or wrong argument count exits 4 with INPUT_UNAVAILABLE. No extra stdout, stderr, traceback or environment output.
Use the configured owned-check and repository/base commit. Require explicit Web result acceptance and disable automatic apply.
Contract is a required immutable input for both branches; both selected branch code results are required inputs for integration.
Keep max attempts 1. Do substantive implementation and tests, with no artificial waits or fixed sleeps.`;

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function same(left: unknown, right: unknown) { return JSON.stringify(left) === JSON.stringify(right); }
function remember(native: NativeFixture, key: string, value: Json) {
  if (native.ids[key] !== undefined && !same(native.ids[key], value)) throw new Error("native_real_original_identity_changed");
  native.ids[key] = value;
}
function items(value: unknown): ShapeItem[] {
  if (!Array.isArray(value) || value.length === 0 || value.some(item => !object(item) || typeof item.key !== "string" || typeof item.title !== "string" || !item.title.trim())) {
    throw new Error("native_real_proposal_shape_refused");
  }
  return value as ShapeItem[];
}

export function realRepositoryReference(native: Pick<NativeFixture, "manifest" | "python">): string {
  if (native.manifest.mode !== "real") throw new Error("native_real_repository_reference_required");
  const reference = native.python(`import json, sys
from pathlib import Path
from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
print(json.dumps(opaque_repository_ref(Path(json.load(sys.stdin)["directory"]))))`, {
    directory: native.manifest.repository_directory,
  });
  if (typeof reference !== "string") throw new Error("native_real_repository_reference_required");
  return reference;
}

export function realProposalShape(proposal: Proposal, selected: RealCase) {
  if (proposal.proposal_schema_version !== 6 || !object(proposal.display)) throw new Error("native_real_proposal_schema_required");
  const workers = items(proposal.display.worker_items), subtasks = items(proposal.display.subtasks);
  const expected = selected === "main" ? ["contract", "cli", "tests", "integration"] : [`${selected}-work`];
  if (!same(workers.map(item => item.key).sort(), [...expected].sort()) || new Set(subtasks.map(item => item.key)).size !== subtasks.length ||
    workers.some(worker => !subtasks.some(task => task.key === worker.subtask_key))) throw new Error("native_real_worker_keys_required");
  if (!object(proposal.display.integration_spec) || proposal.display.integration_spec.integration_worker_item_key !== (selected === "main" ? "integration" : expected[0])) {
    throw new Error("native_real_integration_required");
  }
  if (selected === "main") {
    const edges = proposal.display.worker_item_dependencies;
    const required = ["contract:cli", "contract:tests", "cli:integration", "tests:integration"].sort();
    if (!Array.isArray(edges) || edges.some(edge => !object(edge) || edge.kind !== "hard") ||
      !same(edges.map(edge => `${edge.upstream_worker_item_key}:${edge.downstream_worker_item_key}`).sort(), required)) throw new Error("native_real_dependencies_required");
    for (const [consumer, sources] of [["cli", ["contract"]], ["tests", ["contract"]], ["integration", ["cli", "tests"]]] as const) {
      const groups = proposal.collaboration.input_groups.filter(group => group.consumer_worker_item_key === consumer);
      if (groups.length !== 1 || groups[0]!.mode !== "all" || groups[0]!.requirements.execution_status !== "succeeded" ||
        !same(groups[0]!.candidates.map(item => item.worker_item_key).sort(), [...sources].sort())) throw new Error("native_real_code_inputs_required");
    }
  }
  return { workers, subtasks };
}

export function realRunOverlap(left: NativeTrace, right: NativeTrace): "OBSERVED" | "NOT_OBSERVED" {
  const lifetime = (trace: NativeTrace) => {
    const start = trace.events.find(event => event.kind === "run.started");
    const end = trace.events.find(event => event.kind === "run.closed");
    if (!start || !end) return null;
    const interval = [Date.parse(start.occurred_at), Date.parse(end.occurred_at)] as const;
    return interval.every(Number.isFinite) && interval[1] >= interval[0] ? interval : null;
  };
  const a = lifetime(left), b = lifetime(right);
  return a && b && Math.max(a[0], b[0]) < Math.min(a[1], b[1]) ? "OBSERVED" : "NOT_OBSERVED";
}

export function realInputAdmitted(trace: NativeTrace, operationId: string): boolean {
  const accepted = trace.events.find(event => event.kind === "input.accepted" && event.operation_id === operationId && event.run_id === trace.run_id);
  return !!accepted?.response_id && trace.events.some(event => event.kind === "response.started" && event.operation_id === operationId &&
    event.run_id === trace.run_id && event.response_id === accepted.response_id && event.sequence > accepted.sequence);
}

export async function originalCommand(native: NativeFixture, key: string, timeout = 120_000): Promise<CP["schemas"]["CommandStatusResponse"]> {
  const id = native.id(key);
  const path = native.prefix() + `commands/${id}`;
  let command: CP["schemas"]["CommandStatusResponse"] | null = null;
  await expect.poll(async () => {
    let reply: CP["schemas"]["CommandStatusResponse"];
    try { reply = await native.get<CP["schemas"]["CommandStatusResponse"]>(path); }
    catch { return false; }
    if (reply?.command_id !== id || !["applied", "rejected"].includes(reply.status)) return false;
    command = reply;
    return true;
  }, { timeout }).toBe(true);
  if (!command) throw new Error("native_original_command_unavailable");
  return command;
}

async function bindTaskTools(native: NativeFixture, created: ProfileRef, target: boolean) {
  const { page, manifest } = native;
  await page.getByRole("navigation", { name: "Profile 视图", exact: true }).getByRole("link", { name: "能力", exact: true }).click();
  await page.getByRole("link", { name: "MCP", exact: true }).click();
  if (!target) {
    const connections = await native.get<Product["schemas"]["ToolConnectionView"][]>(`/product/v1/workspaces/${manifest.workspace_id}/tool-connections`);
    const matches = connections.filter(item => item.workspace_id === manifest.workspace_id && item.name === "SIDERA Task Tools" && item.availability === "configured");
    expect(matches).toHaveLength(1);
    remember(native, "task_tool_connection_id", matches[0]!.connection_id);
    native.record("pending", { advertised_task_tools: true });
    await page.getByRole("link", { name: "新建MCP资源", exact: true }).click();
    await page.getByRole("combobox", { name: "受信连接", exact: true }).selectOption(native.id("task_tool_connection_id"));
    await page.getByRole("textbox", { name: "资源名称", exact: true }).fill("SIDERA Task Tools");
    await page.getByRole("button", { name: "保存资源", exact: true }).click();
    await expect.poll(() => typeof native.ids.task_tool_resource_ref).toBe("object");
    await page.getByRole("link", { name: "MCP", exact: true }).click();
  }
  const resource = native.ids.task_tool_resource_ref as Product["schemas"]["ResourceRef"];
  expect(resource.workspace_id).toBe(manifest.workspace_id); expect(resource.revision).toBe(1);
  const row = page.locator(".agent-resource-row").filter({ has: page.getByText(resource.resource_id, { exact: true }) });
  await row.getByRole("button", { name: "绑定 SIDERA Task Tools R1", exact: true }).click();
  await page.getByRole("button", { name: "保存 Profile", exact: true }).click();
  const prefix = target ? "target_" : "";
  await expect.poll(() => (native.ids[`${prefix}profile_ref`] as ProfileRef | undefined)?.revision).toBe(2);
  const ref = native.ids[`${prefix}profile_ref`] as ProfileRef;
  expect(ref).toEqual({ ...created, revision: 2 });
  const view = await native.get<Product["schemas"]["ProfileView"]>(`/agents/v1/workspaces/${manifest.workspace_id}/profiles/${ref.profile_id}?revision=2`);
  const config = loadRealConfig(manifest);
  expect(view.profile.ref).toEqual(ref); expect(view.status.enabled).toBe(true);
  expect(view.profile.draft.model_id).toBe(config.real.model_id);
  expect(view.profile.draft.native_settings).toEqual({ provider_env: "OPENCODE_API_KEY", native_tools: "write" });
  expect(view.profile.draft.max_active).toBe(4); expect(view.profile.draft.environment).toEqual({});
  expect(view.profile.draft.secret_refs).toEqual([{ credential_ref: native.ids.credential_ref, destination: "OPENCODE_API_KEY" }]);
  expect(view.profile.draft.resource_refs).toEqual([resource]);
  return ref;
}

async function createProfile(native: NativeFixture, target: boolean) {
  const { page, manifest } = native, config = loadRealConfig(manifest);
  native.configurationCapture = target ? "target" : "primary";
  try {
    await page.goto(native.path("agents/new"));
    await page.getByRole("textbox", { name: "名称", exact: true }).fill(target ? "Real alternate profile" : "Real role-neutral profile");
    await page.getByRole("combobox", { name: "运行环境", exact: true }).selectOption(native.id("runtime_id"));
    await page.getByRole("textbox", { name: "指令", exact: true }).fill(ROLE_INSTRUCTIONS);
    await page.getByRole("textbox", { name: "Model", exact: true }).fill(config.real.model_id);
    await page.getByRole("textbox", { name: "Provider credential environment", exact: true }).fill(config.real.provider_env);
    await page.getByRole("combobox", { name: "Native coding tools", exact: true }).selectOption("write");
    await page.getByRole("spinbutton", { name: "并行安排上限", exact: true }).fill("4");
    await page.getByRole("button", { name: "配置凭据引用", exact: true }).click();
    const fieldset = page.getByRole("group", { name: "凭据引用", exact: true });
    await fieldset.getByRole("combobox", { name: "凭据引用", exact: true }).selectOption((native.ids.credential_ref as { credential_id: string }).credential_id);
    await fieldset.getByRole("textbox", { name: "注入目标", exact: true }).fill("OPENCODE_API_KEY");
    await fieldset.getByRole("button", { name: "绑定凭据", exact: true }).click();
    await page.getByRole("button", { name: "保存 Profile", exact: true }).click();
    const prefix = target ? "target_" : "";
    await expect.poll(() => typeof native.ids[`${prefix}profile_id`]).toBe("string");
    const created = native.ids[`${prefix}profile_create_ref`] as ProfileRef;
    await expect(page).toHaveURL(manifest.base_url + native.path(`agents/${created.profile_id}?view=settings`));
    return await bindTaskTools(native, created, target);
  } finally { native.configurationCapture = "primary"; }
}

async function configure(native: NativeFixture): Promise<Ids> {
  const { page, manifest } = native, config = loadRealConfig(manifest);
  const credentials = await native.get<Product["schemas"]["CredentialSummarySchema"][]>(`/model-config/v1/workspaces/${manifest.workspace_id}/credentials`);
  const matches = credentials.filter(item => item.workspace_id === manifest.workspace_id && item.alias === config.real.credential_source_alias && item.status === "active" && item.source === "env");
  expect(matches).toHaveLength(1);
  remember(native, "credential_ref", { workspace_id: manifest.workspace_id, credential_id: matches[0]!.credential_id });
  await page.goto(native.path("runtimes"));
  await page.getByRole("combobox", { name: "Harness", exact: true }).selectOption("opencode");
  await page.getByRole("textbox", { name: "可执行文件绝对路径", exact: true }).fill(config.settings.executable);
  await page.getByRole("spinbutton", { name: "活跃 Run 上限", exact: true }).fill("4");
  await page.getByRole("spinbutton", { name: "原生会话上限", exact: true }).fill("8");
  await page.getByRole("button", { name: "登记运行环境", exact: true }).click();
  await expect.poll(() => typeof native.ids.runtime_id).toBe("string");
  const runtime = await native.get<Product["schemas"]["RuntimeSnapshot"]>(`/agents/v1/workspaces/${manifest.workspace_id}/runtimes/${native.id("runtime_id")}`);
  expect(runtime.harness_id).toBe("opencode"); expect(runtime.actual_version).toBe("1.18.32");
  expect(runtime.executable).toBe(config.settings.executable); expect(runtime.limits).toEqual({ max_active: 4, max_sessions: 8 });
  const primary = await createProfile(native, false);
  await page.goto(native.path("agents"));
  await page.getByRole("button", { name: "默认安排", exact: true }).click();
  for (const role of ["Main", "Planner", "Worker"]) await page.getByRole("combobox", { name: `默认 ${role}`, exact: true }).selectOption(primary.profile_id);
  await page.getByRole("button", { name: "保存默认安排", exact: true }).click();
  const defaultsPath = `/product/v1/workspaces/${manifest.workspace_id}/defaults`;
  await expect.poll(async () => (await native.get<Product["schemas"]["DefaultsView"]>(defaultsPath)).version).toBe(1);
  const defaults = await native.get<Product["schemas"]["DefaultsView"]>(defaultsPath);
  expect([defaults.main_profile_id, defaults.planner_profile_id, defaults.worker_profile_id]).toEqual([primary.profile_id, primary.profile_id, primary.profile_id]);
  const target = await createProfile(native, true);
  expect(target.profile_id).not.toBe(primary.profile_id); expect(native.profile()).toEqual(primary);
  expect(await native.get(defaultsPath)).toEqual(defaults);
  const setupPath = `/product/v1/workspaces/${manifest.workspace_id}/setup`;
  expect(await native.get<Setup | null>(setupPath)).toBeNull();
  const csrf = (await page.context().cookies(manifest.base_url)).find(cookie => cookie.name === "sidera_csrf");
  if (!csrf) throw new Error("native_current_csrf_required");
  remember(native, "setup_expected_version", 0);
  native.record("pending", { repository_setup_intent: true, admission: "UNKNOWN" });
  try {
    const reply = await page.request.post(manifest.base_url + setupPath, {
      headers: { Origin: manifest.base_url, "X-Sidera-CSRF": csrf.value },
      data: { repository_directory: manifest.repository_directory, expected_version: 0, planning_budget: config.planning_budget, planner_agent_ref: primary,
        verification_checks: [{ key: "owned-check", version: 1, argv: config.verification.argv, cwd: ".", environment_ref: "env://owned-test",
          prepare_argvs: [], timeout_seconds: 60, success_exit_codes: [0], report_paths: [], required: true }] },
    });
    if (!reply.ok()) throw new Error("native_real_setup_refused");
  } catch {
    // The same original setup is reconciled by readback; it is never submitted twice.
    native.record("pending", { repository_setup_reply: "UNKNOWN" });
  }
  const setup = await native.get<Setup>(setupPath);
  expect(setup.version).toBe(1); expect(setup.workspace_id).toBe(manifest.workspace_id);
  expect(setup.repository_ref).toBe(realRepositoryReference(native)); expect(setup.planner_agent_ref).toEqual(primary);
  remember(native, "planning_policy_id", setup.planning_policy_ref.policy_id);
  native.versions.defaults = defaults.version; native.versions.setup = setup.version; native.versions.planning_policy = setup.planning_policy_ref.version;
  return { ui_runtime: true, ui_profile: true, ui_credential_reference: true, ui_mcp_binding: true, ui_defaults: true, separate_target_profile: true };
}

async function conversation(native: NativeFixture): Promise<Ids> {
  await native.page.goto(native.path("chat"));
  await native.page.getByRole("button", { name: "使用该 Profile 新建对话", exact: true }).click();
  await expect.poll(() => typeof native.ids.thread_id).toBe("string");
  const thread = await native.get<Product["schemas"]["ThreadView"]>(`/main-agent/v1/threads/${native.id("thread_id")}`);
  expect(thread.profileRef).toEqual(native.profile()); expect(thread.turns).toEqual([]); expect(thread.runId).toBeNull();
  return { actual_thread: true, no_run_before_turn: true };
}

async function submitGoal(native: NativeFixture, goal: string): Promise<Ids> {
  await native.page.goto(native.path(`chat/${native.id("thread_id")}`));
  await native.page.getByRole("textbox", { name: "给 Main Agent 的消息" }).fill(goal);
  await native.page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(async () => {
    const thread = await native.get<Product["schemas"]["ThreadView"]>(`/main-agent/v1/threads/${native.id("thread_id")}`);
    const original = thread.turns.find(turn => turn.operationId === native.ids.turn_operation_id);
    if (original) remember(native, "turn_id", original.id);
    return typeof native.ids.turn_id;
  }).toBe("string");
  const turnPath = `/main-agent/v1/threads/${native.id("thread_id")}/turns/${native.id("turn_id")}`;
  await expect.poll(async () => (await native.get<Product["schemas"]["TurnView"]>(turnPath)).status, { timeout: 900_000 }).toBe("completed");
  const turn = await native.get<Product["schemas"]["TurnView"]>(turnPath);
  expect(turn.operationId).toBe(native.id("turn_operation_id")); expect(turn.profileRef).toEqual(native.profile());
  const receipts = turn.items.filter(item => item.type === "toolCall" && item.data.receiptSource === "sidera.product.task-receipt.v1" &&
    item.data.name === "sidera_submit_goal" && item.data.verified === true && item.data.outcome === "applied").map(item => item.data);
  expect(receipts).toHaveLength(1);
  const receipt = receipts[0]!;
  for (const [field, key] of [["rootTaskId", "root_task_id"], ["planningRequestId", "planning_request_id"], ["commandId", "goal_command_id"],
    ["runId", "chat_run_id"], ["responseId", "chat_response_id"]]) {
    if (typeof receipt[field!] !== "string") throw new Error("native_real_verified_receipt_required");
    remember(native, key!, receipt[field!] as string);
  }
  expect(turn.runId).toBe(native.id("chat_run_id")); expect(turn.responseId).toBe(native.id("chat_response_id"));
  expect((await originalCommand(native, "goal_command_id")).status).toBe("applied");
  const link = native.page.locator(`.tool-result-card a[href="${native.path(`tasks/${native.id("root_task_id")}`)}"]`);
  await expect(link).toBeVisible(); await link.click();
  await expect(native.page).toHaveURL(native.manifest.base_url + native.path(`tasks/${native.id("root_task_id")}`));
  return { actual_verified_goal_receipt: true };
}

export async function proposalReady(native: NativeFixture, selected: RealCase): Promise<Ids> {
  const path = native.prefix() + `planning-requests/${native.id("planning_request_id")}`;
  const preserved: Json[] = [];
  for (let repair = 0; repair <= 2; repair++) {
    await expect.poll(async () => (await native.get<Planning>(path)).status, { timeout: 900_000 }).toBe("proposal_ready");
    const planning = await native.get<Planning>(path);
    if (!planning.current_proposal_revision || !planning.current_proposal_content_hash) throw new Error("native_real_proposal_required");
    const proposal = await native.get<Proposal>(path + `/proposals/${planning.current_proposal_revision}`);
    expect(proposal.content_hash).toBe(planning.current_proposal_content_hash); expect(proposal.planning_request_id).toBe(planning.id);
    preserved.push({ id: proposal.id, revision: proposal.revision, content_hash: proposal.content_hash });
    native.record("pending", { original_proposals: preserved });
    await native.page.goto(native.path(`planning/${planning.id}`));
    try {
      realProposalShape(proposal, selected);
      remember(native, "proposal_id", proposal.id); remember(native, "proposal_revision", proposal.revision);
      remember(native, "proposal_content_hash", proposal.content_hash);
      native.versions.planning_request = planning.version;
      return { actual_proposal_schema: 6, preserved_proposals: preserved, current_proposal_revision: proposal.revision };
    } catch {
      if (repair === 2) throw new Error("native_real_proposal_repair_exhausted");
      await native.page.getByRole("button", { name: "要求修改", exact: true }).click();
      await native.page.getByRole("textbox", { name: "修改意见", exact: true }).fill(selected === "main"
        ? "Keep this original Task and repository contract. Repair schema 6 to exactly contract, cli, tests, integration; hard edges contract->cli, contract->tests, cli->integration, tests->integration. Use one all-required immutable code-result input group for each consumer: cli=[contract], tests=[contract], integration=[cli,tests]. Keep explicit confirmation and configured owned-check."
        : `Keep this original Task and substantive workload. Repair schema 6 to exactly one WorkerItem key ${selected}-work; keep explicit confirmation and the existing repository contract.`);
      await native.page.getByRole("button", { name: "提交修改意见", exact: true }).click();
      await expect.poll(async () => (await native.get<Planning>(path)).current_proposal_revision, { timeout: 900_000 }).toBeGreaterThan(proposal.revision);
    }
  }
  throw new Error("native_real_proposal_required");
}

async function readProposal(native: NativeFixture) {
  const proposal = await native.get<Proposal>(native.prefix() + `planning-requests/${native.id("planning_request_id")}/proposals/${String(native.ids.proposal_revision)}`);
  expect(proposal.id).toBe(native.id("proposal_id")); expect(proposal.content_hash).toBe(native.ids.proposal_content_hash);
  return proposal;
}

async function confirmPlan(native: NativeFixture, selected: RealCase): Promise<Ids> {
  const proposal = await readProposal(native), shape = realProposalShape(proposal, selected), { page } = native;
  const before = await native.get<Planning>(native.prefix() + `planning-requests/${native.id("planning_request_id")}`);
  expect(before.status).toBe("proposal_ready");
  const overview = await native.get<CP["schemas"]["TaskConsoleOverviewProjectionResponse"]>(`/api/v1/tasks/${native.id("root_task_id")}/overview?workspace_id=${native.manifest.workspace_id}`);
  expect(overview.root_task_id).toBe(native.id("root_task_id")); expect(overview.workspace_id).toBe(native.manifest.workspace_id);
  expect(overview.subtask_count).toBe(0); expect(overview.running_subtask_count).toBe(0);
  expect(await native.get<CP["schemas"]["ExecutionResponse"][]>(`/api/v1/executions?workspace_id=${native.manifest.workspace_id}&task_id=${native.id("root_task_id")}`)).toEqual([]);
  native.record("pending", { canonical_workers_before_confirmation: 0 });
  await page.goto(native.path(`planning/${native.id("planning_request_id")}`));
  for (const subtask of shape.subtasks) await page.getByRole("combobox", { name: `${subtask.title} owner`, exact: true }).selectOption("human");
  for (const worker of shape.workers) {
    const ref = selected === "main" && worker.key === "tests" ? native.ids.target_profile_ref as ProfileRef : native.profile();
    await page.getByRole("combobox", { name: `${worker.title} owner`, exact: true }).selectOption(JSON.stringify(["profile", ref.profile_id, ref.workspace_id, ref.revision]));
  }
  await page.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect.poll(() => typeof native.ids.confirmation_command_id).toBe("string");
  await expect.poll(async () => (await originalCommand(native, "confirmation_command_id")).status, { timeout: 120_000 }).toBe("applied");
  const materialized = await native.get<Materialization>(native.prefix() + `planning-requests/${native.id("planning_request_id")}/materialization`);
  expect(materialized.proposal_revision_id).toBe(proposal.id); expect(materialized.root_task_id).toBe(native.id("root_task_id"));
  expect(Object.keys(materialized.worker_item_mapping).sort()).toEqual(shape.workers.map(item => item.key).sort());
  for (const [key, value] of Object.entries({ materialization_id: materialized.id, plan_snapshot_id: materialized.plan_snapshot_id,
    plan_generation: materialized.plan_generation, worker_item_mapping: materialized.worker_item_mapping })) remember(native, key, value);
  const sourceKey = selected === "main" ? "cli" : `${selected}-work`;
  remember(native, "source_worker_key", sourceKey); remember(native, "source_worker_item_id", materialized.worker_item_mapping[sourceKey]!);
  for (const worker of shape.workers) {
    const binding = await native.get<CP["schemas"]["AgentBindingView"]>(native.prefix() + `assignments/worker_item/${materialized.worker_item_mapping[worker.key]}`);
    const ref = selected === "main" && worker.key === "tests" ? native.ids.target_profile_ref : native.profile();
    expect(binding.profile_ref).toEqual(ref); expect(binding.assignee).toEqual({ kind: "profile", id: (ref as ProfileRef).profile_id });
  }
  for (const subtask of shape.subtasks) {
    const id = materialized.subtask_task_mapping[subtask.key]!;
    const binding = await native.get<CP["schemas"]["AgentBindingView"]>(native.prefix() + `assignments/subtask/${id}`);
    expect(binding.assignee).toEqual({ kind: "human", id: native.manifest.principal_id }); expect(binding.profile_ref).toBeNull();
  }
  remember(native, "subtask_key", shape.subtasks[0]!.key);
  remember(native, "subtask_id", materialized.subtask_task_mapping[shape.subtasks[0]!.key]!);
  return { explicit_subtask_human: true, explicit_worker_profiles: true, materialization_id: materialized.id };
}

async function workerObservation(native: NativeFixture, workerItemId: string): Promise<WorkerObservation | null> {
  const context = await native.get<NativeContext>(native.prefix() + `worker-items/${workerItemId}/native-context`);
  if (!context.execution || !context.run) return null;
  const pin = currentNativeTracePin(context);
  const workspace = await native.get<WorkerView>(native.prefix() + `worker-items/${workerItemId}/workspace`);
  const execution = workspace.execution_history.find(item => item.execution_id === pin.execution_id);
  if (!execution) return null;
  const trace = await readRealNativeTrace(native, pin);
  expect(context.run.actual_runtime_version).toBe("1.18.32"); expect(context.run.selected_model).toBe("opencode-go/deepseek-v4.1-flash");
  return { pin, session_id: execution.session_id, context, started_at: execution.started_at, finished_at: execution.finished_at, status: execution.status, trace };
}

function retainSource(native: NativeFixture, source: WorkerObservation) {
  for (const [key, value] of Object.entries({ source_execution_id: source.pin.execution_id, source_run_id: source.pin.run_id,
    source_session_id: source.session_id, binding_version: source.context.execution!.binding_version })) if (value !== null) remember(native, key, value);
  if (source.context.context) remember(native, "source_context_version", source.context.context.context_version);
  native.ids.original_trace_cursor = source.trace.next_sequence;
}

async function sendInput(native: NativeFixture): Promise<{ status: "passed" | "failed" | "incomplete"; observed: Ids }> {
  const { page } = native;
  const before = await native.get<NativeContext>(native.prefix() + `worker-items/${native.id("source_worker_item_id")}/native-context`);
  if (!before.input_enabled || !before.context || before.context.run_id !== native.ids.source_run_id) {
    return { status: "failed", observed: { admission: "NOT_ADMITTED", closed_rejection: "CURRENT_CONTEXT_CLOSED", same_run_delivery: false } };
  }
  await page.goto(native.path(`worker-items/${native.id("source_worker_item_id")}`));
  const text = native.realCase === "main"
    ? "Check bool port rejection, blank name, enabled=false and port boundaries explicitly. Preserve the immutable contract and verifier. Keep stdout to one JSON object and stderr empty."
    : "Add coverage for empty and duplicate entries, Unicode names and round-trip stability. Preserve the seed verifier and inputs; use standard library only.";
  await page.getByRole("textbox", { name: "给当前 Worker 的指令" }).fill(text);
  await page.getByRole("button", { name: "复核当前 context", exact: true }).click();
  const send = page.getByRole("button", { name: "发送到当前 Session", exact: true });
  if (!await send.isEnabled()) return { status: "failed", observed: { admission: "NOT_ADMITTED", closed_rejection: "WEB_SEND_DISABLED", same_run_delivery: false } };
  await send.click();
  if (typeof native.ids.input_command_id !== "string") return { status: "incomplete", observed: { admission: "UNKNOWN", same_run_delivery: false } };
  const command = await originalCommand(native, "input_command_id");
  if (command.status === "rejected") return { status: "failed", observed: { original_command_status: "rejected", same_run_delivery: false } };
  await expect.poll(async () => (await originalCommand(native, "input_command_id")).status, { timeout: 120_000 }).toBe("applied");
  const applied = await originalCommand(native, "input_command_id");
  if (applied.result?.aggregate_id) remember(native, "input_id", applied.result.aggregate_id);
  const path = `/api/v1/gateway/inputs?workspace_id=${native.manifest.workspace_id}&execution_id=${native.id("source_execution_id")}&session_id=${native.id("source_session_id")}`;
  await expect.poll(async () => (await native.get<CP["schemas"]["ExecutionInputPageResponse"]>(path)).items.find(item => item.id === native.ids.input_id)?.status,
    { timeout: 900_000 }).toBe("delivered");
  const row = (await native.get<CP["schemas"]["ExecutionInputPageResponse"]>(path)).items.find(item => item.id === native.ids.input_id)!;
  if (!row.operation_id) throw new Error("native_real_input_operation_required");
  remember(native, "input_operation_id", row.operation_id);
  expect(row.execution_id).toBe(native.id("source_execution_id")); expect(row.session_id).toBe(native.id("source_session_id"));
  const pin = currentNativeTracePin(before);
  await expect.poll(async () => {
    const trace = await readRealNativeTrace(native, pin);
    native.ids.input_trace_cursor = trace.next_sequence;
    return realInputAdmitted(trace, native.id("input_operation_id"));
  }, { timeout: 900_000 }).toBe(true);
  const after = await native.get<NativeContext>(native.prefix() + `worker-items/${native.id("source_worker_item_id")}/native-context`);
  expect(after.execution?.run_id).toBe(native.id("source_run_id"));
  await expect.poll(async () => {
    const activity = await native.get<CP["schemas"]["WorkerItemActivityPageResponse"]>(native.prefix() + `worker-items/${native.id("source_worker_item_id")}/activity?limit=100`);
    return activity.items.some(item => item.source_type === "execution_input" && item.source_id === native.ids.input_id && item.delivery_status === "delivered");
  }).toBe(true);
  await page.reload();
  await expect(page.getByRole("heading", { name: "工作记录", exact: true })).toBeVisible();
  await expect(page.getByText(text, { exact: true }).first()).toBeVisible();
  return { status: "passed", observed: { actual_native_admission: true, same_run_delivery: true, input_trace_cursor: native.ids.input_trace_cursor!,
    visible_in_history: true, token_interrupt: "NOT_CLAIMED" } };
}

async function dependencyExecution(native: NativeFixture): Promise<Ids> {
  const mapping = native.ids.worker_item_mapping as Record<string, string>;
  const observations: Record<string, WorkerObservation> = {};
  let inputAttempted = native.ids.main_input_attempted === true;
  await expect.poll(async () => {
    for (const key of ["contract", "cli", "tests", "integration"]) {
      const value = await workerObservation(native, mapping[key]!);
      if (value) observations[key] = value;
    }
    const source = observations.cli;
    if (source) {
      retainSource(native, source);
      if (!inputAttempted && source.context.input_enabled) {
        inputAttempted = true; native.ids.main_input_attempted = true;
        const previous = native.stage; native.stage = "worker_input";
        native.record("pending", { input_intent: true });
        try { const input = await sendInput(native); native.record(input.status, input.observed); }
        catch { native.record("incomplete", { admission: "UNKNOWN", same_run_delivery: false }); }
        finally { native.stage = previous; }
      }
    }
    return ["contract", "cli", "tests", "integration"].every(key => observations[key]?.status === "succeeded");
  }, { timeout: 900_000, intervals: [250, 1000, 2000] }).toBe(true);
  retainSource(native, observations.cli!);
  if (!inputAttempted) {
    const previous = native.stage; native.stage = "worker_input";
    native.ids.main_input_attempted = true;
    native.record("failed", { admission: "NOT_ADMITTED", closed_rejection: "ACTIVE_WINDOW_MISSED", same_run_delivery: false });
    native.stage = previous;
  }
  for (const [upstream, downstream] of [["contract", "cli"], ["contract", "tests"], ["cli", "integration"], ["tests", "integration"]]) {
    const end = observations[upstream!]!.finished_at, start = observations[downstream!]!.started_at;
    if (!end || !start) throw new Error("native_real_dependency_times_required");
    expect(Date.parse(end)).toBeLessThanOrEqual(Date.parse(start));
  }
  const overlap = realRunOverlap(observations.cli!.trace, observations.tests!.trace);
  const inputs: Json[] = [];
  for (const key of ["cli", "tests", "integration"]) {
    const execution = observations[key]!.pin.execution_id;
    const context = await native.get<{ code_input_manifest: { workspace_id: string; execution_id: string; input_hash: string;
      selection_id: string; selection_hash: string; sources: { result_id: string; result_commit: string; result_tree: string }[] } }>(native.prefix() + `executions/${execution}/context-package?manifest_schema_version=3`);
    const code = context.code_input_manifest;
    expect(code.workspace_id).toBe(native.manifest.workspace_id); expect(code.execution_id).toBe(execution);
    expect(code.sources).toHaveLength(key === "integration" ? 2 : 1);
    expect(code.input_hash).toMatch(/^[0-9a-f]{64}$/); expect(code.selection_hash).toMatch(/^[0-9a-f]{64}$/);
    inputs.push({ worker_key: key, execution_id: execution, input_hash: code.input_hash, selection_id: code.selection_id, selection_hash: code.selection_hash,
      sources: code.sources as unknown as Json });
  }
  const safe: Ids = {};
  for (const [key, value] of Object.entries(observations)) safe[key] = { ...value.pin, session_id: value.session_id, status: value.status,
    started_at: value.started_at, finished_at: value.finished_at, trace_cursor: value.trace.next_sequence, observed_model: value.context.run?.observed_model ?? "NOT_OBSERVED" };
  native.ids.worker_observations = safe;
  return { actual_workers: safe, code_inputs: inputs, dependency_order_observed: true, parallel_overlap: overlap };
}

async function resultVerified(native: NativeFixture): Promise<Ids> {
  const path = native.prefix() + `tasks/${native.id("root_task_id")}/results`;
  await expect.poll(async () => (await native.get<ResultOverview>(path)).result_stage, { timeout: 900_000 }).toBe("awaiting_acceptance");
  const overview = await native.get<ResultOverview>(path), candidate = overview.current_candidate;
  if (!candidate?.result_commit || !candidate.result_tree) throw new Error("native_real_candidate_required");
  expect(overview.acceptance).toBeNull(); expect(overview.task_status).not.toBe("succeeded");
  const detail = await native.get<ResultDetail>(path + `/${candidate.candidate_id}`);
  const checks = detail.verifications.filter(check => check.check_key === "owned-check" && check.status === "passed" && check.exit_code === 0);
  expect(checks).toHaveLength(1);
  const check = checks[0]!;
  expect(check.actual_commit).toBe(candidate.result_commit); expect(check.actual_tree).toBe(candidate.result_tree);
  for (const [key, value] of Object.entries({ candidate_id: candidate.candidate_id, candidate_hash: candidate.candidate_hash,
    result_commit: candidate.result_commit, result_tree: candidate.result_tree, verification_id: check.verification_id })) remember(native, key, value);
  native.versions.task = overview.task_version; native.versions.result = overview.result_version;
  return { immutable_candidate_verified: true, exit_code: 0, repository_proof: native.realRepositoryProof(detail.candidate, detail.verifications) };
}

async function resultAccepted(native: NativeFixture): Promise<Ids> {
  const { page } = native, root = native.id("root_task_id"), path = native.prefix() + `tasks/${root}/results`;
  const before = await native.get<ResultOverview>(path);
  expect(before.current_candidate?.candidate_id).toBe(native.id("candidate_id")); expect(before.apply_policy?.apply_on_accept).toBe(false);
  await page.goto(native.path(`tasks/${root}`)); await page.getByRole("tab", { name: "成果", exact: true }).click();
  for (const criterion of before.criteria) await page.getByRole("combobox", { name: `${criterion.text} 的决定`, exact: true }).selectOption("accept_passed");
  await page.getByRole("button", { name: "接受成果", exact: true }).click();
  await expect.poll(() => typeof native.ids.acceptance_command_id).toBe("string");
  await expect.poll(async () => (await native.get<ResultOverview>(path)).task_status, { timeout: 120_000 }).toBe("succeeded");
  const result = await native.get<ResultOverview>(path);
  expect(result.acceptance?.candidate_id).toBe(native.id("candidate_id")); expect(result.acceptance?.candidate_hash).toBe(native.ids.candidate_hash);
  expect(result.acceptance?.command_id).toBe(native.id("acceptance_command_id"));
  expect((await originalCommand(native, "acceptance_command_id")).status).toBe("applied");
  const detail = await native.get<ResultDetail>(path + `/${native.id("candidate_id")}`);
  expect(detail.apply_operations).toEqual([]);
  return { actual_finalization_succeeded: true, repository_proof: native.realRepositoryProof(detail.candidate, detail.verifications) };
}

async function navigationHistory(native: NativeFixture): Promise<Ids> {
  const { page } = native, root = native.id("root_task_id"), worker = native.id("source_worker_item_id");
  const shape = realProposalShape(await readProposal(native), "main");
  const source = shape.workers.find(item => item.key === "cli")!;
  const subtask = shape.subtasks.find(item => item.key === source.subtask_key)!;
  const screenshots: Json[] = [], mutationCount = native.mutations.length;
  await page.goto(native.path(`tasks/${root}`));
  await page.getByRole("tab", { name: "看板", exact: true }).click();
  await expect(page.getByRole("region", { name: "子任务状态看板", exact: true })).toBeVisible();
  screenshots.push(await native.screenshot("real-board"));
  await page.getByRole("tab", { name: "DAG", exact: true }).click();
  await expect(page.getByRole("region", { name: "任务 DAG", exact: true })).toBeVisible();
  screenshots.push(await native.screenshot("real-dag"));
  await page.getByRole("tab", { name: "看板", exact: true }).click();
  await page.getByRole("button", { name: `打开子任务：${subtask.title}`, exact: true }).click();
  const subtaskUrl = page.url();
  await page.getByRole("button", { name: `打开 Worker：${source.title}`, exact: true }).click();
  await expect(page).toHaveURL(native.manifest.base_url + native.path(`worker-items/${worker}`));
  const tabs = page.getByRole("navigation", { name: "已打开的任务" });
  await tabs.locator(".workspace-tab.is-active").getByRole("button", { name: /^关闭/ }).click();
  await expect(page).toHaveURL(subtaskUrl);
  await page.getByRole("button", { name: `打开 Worker：${source.title}`, exact: true }).click();
  await page.reload();
  await expect(page.getByRole("heading", { name: "工作记录", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Agent Trace", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "查看当前 Agent Trace", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Agent Trace", exact: true });
  await dialog.getByText("原始执行", { exact: true }).click();
  await expect(dialog.getByText(native.id("source_run_id"), { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭 Agent Trace", exact: true }).click();
  const pin = { workspace_id: native.manifest.workspace_id, worker_item_id: worker, execution_id: native.id("source_execution_id"), run_id: native.id("source_run_id"), profile_ref: native.profile() };
  const trace = await readRealNativeTrace(native, pin);
  expect(trace.events.length).toBeGreaterThan(0);
  native.ids.original_trace_cursor = trace.next_sequence;
  await page.goto(native.path(`tasks/${root}/executions/${pin.execution_id}/sessions/${native.id("source_session_id")}/trace`));
  await expect(page.getByRole("region", { name: "Worker Trace 页面", exact: true })).toBeVisible();
  await page.getByText("原始执行", { exact: true }).click();
  await expect(page.getByText(pin.run_id, { exact: true })).toBeVisible();
  screenshots.push(await native.screenshot("real-history-trace"));
  const notifications = await native.get<CP["schemas"]["NotificationThreadPageResponse"]>(native.prefix() + "notification-threads?category=progress&limit=100");
  const related = notifications.items.find(item => item.root_task_id === root && item.deep_link !== null);
  if (related) {
    await page.goto(native.path("inbox")); await page.getByRole("tab", { name: /^进度通报/ }).click();
    await page.locator(`button[aria-controls="notification-thread-history-${related.id}"]`).click();
    const history = page.locator(`#notification-thread-history-${related.id}`);
    await history.getByRole("button", { name: related.worker_item_id ? "打开 WorkerItem" : related.subtask_id ? "打开子任务" : "打开任务", exact: true }).click();
    await expect(page).toHaveURL(new URL(related.deep_link!, native.manifest.base_url).href);
  }
  for (const viewport of [{ width: 1440, height: 960 }, { width: 1366, height: 900 }]) {
    await page.setViewportSize(viewport);
    for (const path of [`tasks/${root}`, `worker-items/${worker}`]) {
      await page.goto(native.path(path)); screenshots.push(await native.screenshot(path.startsWith("tasks") ? "real-task-tabs" : "real-worker-tabs"));
    }
  }
  expect(native.mutations.length).toBe(mutationCount);
  return { screenshots, ordinary_task_navigation: true, original_trace_cursor: trace.next_sequence,
    related_notification: related?.id ?? "NOT_OBSERVED", knowledge_scope: "NOT_CONFIGURED" };
}

async function waitFocusedSource(native: NativeFixture) {
  let source: WorkerObservation | null = null;
  await expect.poll(async () => {
    source = await workerObservation(native, native.id("source_worker_item_id"));
    return source?.context.input_enabled === true && source.context.context !== null;
  }, { timeout: 900_000, intervals: [250, 500, 1000] }).toBe(true);
  if (!source) throw new Error("native_real_active_window_missed");
  retainSource(native, source);
  native.recordFocusedReal("pending", { actual_active_source: true });
  return source as WorkerObservation;
}

async function focusedHandoff(native: NativeFixture): Promise<Ids> {
  const source = await waitFocusedSource(native), { page } = native, target = native.ids.target_profile_ref as ProfileRef;
  await page.goto(native.path(`worker-items/${native.id("source_worker_item_id")}`));
  const ownership = page.getByRole("region", { name: "归属设置", exact: true });
  await ownership.getByRole("combobox", { name: "设置归属", exact: true }).selectOption(JSON.stringify(["profile", target.profile_id, target.workspace_id, target.revision]));
  const start = ownership.getByRole("button", { name: "开始原生交接", exact: true });
  const reviewCurrent = ownership.getByRole("button", { name: "重新读取并复核", exact: true });
  if (await reviewCurrent.isVisible()) await reviewCurrent.click();
  await expect(start).toBeEnabled(); await start.click();
  const dialog = page.getByRole("dialog", { name: "原生 Profile 交接", exact: true });
  await dialog.getByRole("textbox", { name: "交接原因", exact: true }).fill("Explicit local Profile switch for this original directed workload.");
  await dialog.getByRole("textbox", { name: "交接指令", exact: true }).fill("Continue the substantive catalog implementation and validation from the latest context. Preserve seed files and generation.");
  await dialog.getByRole("button", { name: "准备原生交接", exact: true }).click();
  await expect.poll(() => typeof native.ids.handoff_prepare_command_id).toBe("string");
  const prepared = await originalCommand(native, "handoff_prepare_command_id");
  expect(prepared.status).toBe("applied");
  if (prepared.result?.aggregate_id) remember(native, "handoff_proposal_id", prepared.result.aggregate_id);
  const proposal = await native.get<HandoffProposal>(native.prefix() + `handoff-proposals/${native.id("handoff_proposal_id")}`);
  expect(proposal.source_execution_id).toBe(source.pin.execution_id); expect(proposal.source_run_id).toBe(source.pin.run_id);
  expect(proposal.source_profile_ref).toEqual(native.profile()); expect(proposal.target_profile_ref).toEqual(target);
  expect(proposal.context.source_context_version).toBe(native.ids.source_context_version); expect(proposal.plan_base.generation).toBe(native.ids.plan_generation);
  expect((await native.get<NativeContext>(native.prefix() + `worker-items/${native.id("source_worker_item_id")}/native-context`)).execution?.execution_id).toBe(source.pin.execution_id);
  native.recordFocusedReal("pending", { no_target_before_confirmation: true, proposal_hash: proposal.proposal_hash });
  const review = page.getByRole("dialog", { name: "复核原生交接方案", exact: true });
  await review.getByRole("button", { name: "确认交接", exact: true }).click();
  await expect.poll(() => typeof native.ids.handoff_confirm_command_id).toBe("string");
  const confirmed = await originalCommand(native, "handoff_confirm_command_id");
  expect(confirmed.status).toBe("applied");
  if (confirmed.result?.aggregate_id) remember(native, "handoff_transition_id", confirmed.result.aggregate_id);
  const path = native.prefix() + `handoff-transitions/${native.id("handoff_transition_id")}`;
  await expect.poll(async () => {
    const transition = await native.get<HandoffTransition>(path);
    if (transition.failure_code === "source_stop_unconfirmed") {
      expect(transition.target_execution_id).toBeNull(); expect(transition.target_run_id).toBeNull();
      native.recordFocusedReal("incomplete", { source_stop: "UNKNOWN", phase: transition.phase, writer_state: transition.writer_state });
    }
    return transition.status;
  }, { timeout: 900_000 }).toBe("converged");
  const transition = await native.get<HandoffTransition>(path);
  expect(transition.source_run_id).toBe(source.pin.run_id); expect(transition.proposal_id).toBe(proposal.id);
  expect(transition.plan_base.generation).toBe(native.ids.plan_generation); expect(transition.failure_code).toBeNull();
  if (!transition.target_execution_id || !transition.target_run_id) throw new Error("native_real_target_not_admitted");
  expect(transition.target_execution_id).not.toBe(source.pin.execution_id); expect(transition.target_run_id).not.toBe(source.pin.run_id);
  const stopped = await native.get<{ id: string; status: string }>(`/api/v1/executions/${source.pin.execution_id}`);
  expect(stopped.id).toBe(source.pin.execution_id); expect(stopped.status).toBe("stopped");
  const latest = await native.get<NativeContext>(native.prefix() + `worker-items/${native.id("source_worker_item_id")}/native-context`);
  expect(latest.execution?.execution_id).toBe(transition.target_execution_id); expect(latest.execution?.run_id).toBe(transition.target_run_id);
  expect(latest.run?.profile_ref).toEqual(target); expect(latest.context?.context_version).toBe(transition.target_context_version);
  expect(latest.context?.plan_generation).toBe(native.ids.plan_generation);
  const originalTrace = await readRealNativeTrace(native, source.pin);
  expect(originalTrace.events.length).toBeGreaterThan(0);
  const targetTrace = await readRealNativeTrace(native, currentNativeTracePin(latest));
  expect(targetTrace.events.length).toBeGreaterThan(0);
  remember(native, "target_execution_id", transition.target_execution_id); remember(native, "target_run_id", transition.target_run_id);
  return { source_stop: "CONFIRMED", distinct_target_admitted: true, latest_context_version: transition.target_context_version,
    same_generation: true, original_source_trace_cursor: originalTrace.next_sequence, target_trace_cursor: targetTrace.next_sequence };
}

async function focusedJourney(native: NativeFixture, selected: "input" | "handoff") {
  const previous = realFocusedRecords(native.manifest, selected, native.caseId);
  if (previous.length) {
    await native.queryRealOriginalOperations();
    native.recordFocusedReal("incomplete", { admission: "QUERY_ONLY", previous_status: previous.at(-1)!.status });
    throw new Error("native_real_existing_case_query_only");
  }
  native.profile();
  const goal = `Create a new independent real Task for directed ${selected} validation, using normal SIDERA task tools and explicit human confirmation.
Use schema 6 with exactly one WorkerItem key ${selected}-work. Implement ${selected}_catalog.py and test_${selected}_catalog.py using standard library only.
The catalog must normalize and index at least 2000 generated named records, preserve stable round trips, reject invalid entries and duplicates,
and include substantive unittest coverage for all records, Unicode names, empty collections and malformed entries. Execute that unittest suite.
Do not change README.md, verify_demo.py or inputs. This is an independent catalog workload, not a replay of the JSON CLI task.
Keep max attempts 1, no automatic apply, no fabricated facts, artificial prompt holds, fixed sleeps or reopened Runs.
Use the existing repository, profile defaults and planner setup. Wait for explicit confirmation before executing.`;
  native.stage = "conversation"; native.recordFocusedReal("pending", { selected_directed_workload: true });
  await conversation(native);
  native.stage = "goal_submitted"; await submitGoal(native, goal);
  native.stage = "proposal_ready"; await proposalReady(native, selected);
  native.stage = "plan_confirmed"; await confirmPlan(native, selected);
  native.stage = selected === "input" ? "worker_input" : "navigation_history";
  if (selected === "input") {
    await waitFocusedSource(native);
    const result = await sendInput(native);
    native.recordFocusedReal(result.status, result.observed);
    if (result.status !== "passed") throw new Error("native_real_directed_input_incomplete");
  } else native.recordFocusedReal("passed", await focusedHandoff(native));
}

if (process.env.SIDERA_NATIVE_REAL_ENTRY === "1") {
  const manifestPath = process.env.SIDERA_NATIVE_MANIFEST;
  if (!manifestPath) throw new Error("native_owned_manifest_required");
  const selected = realScenario(loadOwnedManifest(manifestPath, realNavigationRecovery()));
  realTest(`real native ${selected}`, async ({ native }) => {
    await native.authorize();
    if (selected !== "main") { await focusedJourney(native, selected); return; }
    const stages: [Stage, () => Promise<Ids>][] = [
      ["configuration", () => configure(native)], ["conversation", () => conversation(native)],
      ["goal_submitted", () => submitGoal(native, MAIN_GOAL)], ["proposal_ready", () => proposalReady(native, "main")],
      ["plan_confirmed", () => confirmPlan(native, "main")], ["dependency_execution", () => dependencyExecution(native)],
      ["result_verified", () => resultVerified(native)], ["result_accepted", () => resultAccepted(native)],
      ["navigation_history", () => navigationHistory(native)],
    ];
    for (const [stage, action] of stages) await native.step(stage, action);
  });
}
