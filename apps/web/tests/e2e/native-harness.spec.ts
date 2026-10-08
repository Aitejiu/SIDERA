import { test, expect, readProtectedJson, assertNativeNavigation, currentNativeTracePin, exactNativeTrace, loadOwnedManifest, nativeScenario, nativeRecords,
  type NativeFixture, type NativeContext, type Planning, type Materialization, type ResultOverview, type ResultDetail, type Json } from "./native-harness-fixtures.js";
import type { components as CP } from "../../src/shared/api/schema.js";
import type { components as Product } from "../../src/shared/api/product-schema.js";

interface OwnedConfig {
  settings: { workspace_id: string; harness_id: string; executable: string; max_active: number; max_sessions: number };
  planning_budget: Record<string, number>;
}
type Setup = { workspace_id: string; version: number; repository_ref: string;
  planning_policy_ref: { policy_id: string; version: number }; planner_agent_ref: Product["schemas"]["ProfileRef"] };
type NativeProposal = CP["schemas"]["NativeHandoffProposalResponse"];
type NativeTransition = CP["schemas"]["NativeHandoffTransitionResponse"];

const manifestPath = process.env.SIDERA_NATIVE_MANIFEST;
if (!manifestPath) throw new Error("native_owned_manifest_required");
const scenario = nativeScenario(loadOwnedManifest(manifestPath, process.env.SIDERA_NATIVE_RESUME === "1"));

async function bindTaskTools(native: NativeFixture, created: Product["schemas"]["ProfileRef"], target = false) {
  const { page, manifest } = native;
  expect(created.revision).toBe(1);
  await page.getByRole("navigation", { name: "Profile 视图", exact: true }).getByRole("link", { name: "能力", exact: true }).click();
  await page.getByRole("link", { name: "MCP", exact: true }).click();
  if (!target) {
    await page.getByRole("link", { name: "新建MCP资源", exact: true }).click();
    const metadata = await native.get<Product["schemas"]["ToolConnectionView"][]>(`/product/v1/workspaces/${manifest.workspace_id}/tool-connections`);
    expect(metadata).toHaveLength(1);
    const connection = metadata[0]!;
    expect(connection.workspace_id).toBe(manifest.workspace_id); expect(connection.name).toBe("SIDERA Task Tools"); expect(connection.availability).toBe("configured");
    native.ids.task_tool_connection_id = connection.connection_id;
    native.record("pending", { advertised_task_tools: true });
    const select = page.getByRole("combobox", { name: "受信连接", exact: true });
    await expect(select.getByRole("option", { name: "SIDERA Task Tools · configured", exact: true })).toBeEnabled();
    await select.selectOption(connection.connection_id);
    await page.getByRole("textbox", { name: "资源名称", exact: true }).fill("SIDERA Task Tools");
    await page.getByRole("button", { name: "保存资源", exact: true }).click();
    await expect.poll(() => typeof native.ids.task_tool_resource_ref).toBe("object");
    await expect(page.getByText("资源已保存 · 现有绑定未改变", { exact: true })).toBeVisible();
    await page.getByRole("link", { name: "MCP", exact: true }).click();
  }
  const resource = native.ids.task_tool_resource_ref as Product["schemas"]["ResourceRef"];
  expect(resource.workspace_id).toBe(manifest.workspace_id); expect(resource.revision).toBe(1);
  const row = page.locator(".agent-resource-row").filter({ has: page.getByText(resource.resource_id, { exact: true }) });
  await row.getByRole("button", { name: "绑定 SIDERA Task Tools R1", exact: true }).click();
  await page.getByRole("button", { name: "保存 Profile", exact: true }).click();
  const prefix = target ? "target_" : "";
  await expect.poll(() => (native.ids[`${prefix}profile_ref`] as Product["schemas"]["ProfileRef"] | undefined)?.revision).toBe(2);
  const final = native.ids[`${prefix}profile_ref`] as Product["schemas"]["ProfileRef"];
  expect(final).toEqual({ ...created, revision: 2 }); expect(native.ids[`${prefix}profile_create_ref`]).toEqual(created);
  expect(native.ids[`${prefix}configuration_operation_id`]).not.toBe(native.ids[`${prefix}profile_create_operation_id`]);
  const view = await native.get<Product["schemas"]["ProfileView"]>(`/agents/v1/workspaces/${manifest.workspace_id}/profiles/${final.profile_id}?revision=2`);
  const tool = await native.get<Product["schemas"]["ResourceView"]>(`/agents/v1/workspaces/${manifest.workspace_id}/resources/${resource.resource_id}?revision=1`);
  expect(view.profile.ref).toEqual(final); expect(view.status.enabled).toBe(true); expect(view.profile.draft.resource_refs).toEqual([resource]);
  expect(tool.resource.ref).toEqual(resource); expect(tool.status.enabled).toBe(true); expect(tool.resource.draft.kind).toBe("mcp");
  expect(tool.resource.draft.settings).toEqual({ connection_id: native.id("task_tool_connection_id") }); expect(tool.resource.draft.secret_refs).toEqual([]);
  return final;
}

async function configure(native: NativeFixture) {
  const { page, manifest } = native;
  const config = readProtectedJson(manifest.config_path) as OwnedConfig;
  expect(config.settings.workspace_id).toBe(manifest.workspace_id);
  expect(config.settings.max_active).toBe(4); expect(config.settings.max_sessions).toBe(4);
  await page.goto(native.path("runtimes"));
  await page.getByRole("combobox", { name: "Harness", exact: true }).selectOption(config.settings.harness_id);
  await page.getByRole("textbox", { name: "可执行文件绝对路径", exact: true }).fill(config.settings.executable);
  await page.getByRole("spinbutton", { name: "活跃 Run 上限", exact: true }).fill("4");
  await page.getByRole("spinbutton", { name: "原生会话上限", exact: true }).fill("4");
  await page.getByRole("button", { name: "登记运行环境", exact: true }).click();
  await expect.poll(() => typeof native.ids.runtime_id).toBe("string");
  const runtime = await native.get<Product["schemas"]["RuntimeSnapshot"]>(`/agents/v1/workspaces/${manifest.workspace_id}/runtimes/${native.id("runtime_id")}`);
  expect(runtime.harness_id).toBe(config.settings.harness_id); expect(runtime.ref.workspace_id).toBe(manifest.workspace_id);
  expect(runtime.limits.max_active).toBe(4); expect(runtime.limits.max_sessions).toBe(4);
  await page.goto(native.path("agents/new"));
  await page.getByRole("textbox", { name: "名称", exact: true }).fill("Native composed journey");
  await page.getByRole("combobox", { name: "运行环境", exact: true }).selectOption(native.id("runtime_id"));
  await page.getByRole("textbox", { name: "指令", exact: true }).fill("Execute the authorized native work");
  await page.getByRole("spinbutton", { name: "并行安排上限", exact: true }).fill("4");
  await page.getByRole("button", { name: "保存 Profile", exact: true }).click();
  await expect.poll(() => typeof native.ids.profile_id).toBe("string");
  const created = native.profile();
  await expect(page).toHaveURL(manifest.base_url + native.path(`agents/${created.profile_id}?view=settings`));
  const profile = await bindTaskTools(native, created);
  await page.goto(native.path("agents"));
  await page.getByRole("button", { name: "默认安排", exact: true }).click();
  for (const role of ["Main", "Planner", "Worker"]) await page.getByRole("combobox", { name: `默认 ${role}`, exact: true }).selectOption(profile.profile_id);
  await page.getByRole("button", { name: "保存默认安排", exact: true }).click();
  await expect.poll(async () => (await native.get<Product["schemas"]["DefaultsView"]>(`/product/v1/workspaces/${manifest.workspace_id}/defaults`)).version).toBe(1);
  const defaults = await native.get<Product["schemas"]["DefaultsView"]>(`/product/v1/workspaces/${manifest.workspace_id}/defaults`);
  expect(defaults.workspace_id).toBe(manifest.workspace_id);
  expect([defaults.main_profile_id, defaults.planner_profile_id, defaults.worker_profile_id]).toEqual([profile.profile_id, profile.profile_id, profile.profile_id]);
  // Only the repository/check prerequisite lacks a Web writer.
  const setupPath = `/product/v1/workspaces/${manifest.workspace_id}/setup`;
  expect(await native.get<Setup | null>(setupPath)).toBeNull();
  const csrf = (await page.context().cookies(manifest.base_url)).find(cookie => cookie.name === "sidera_csrf");
  if (!csrf) throw new Error("native_current_csrf_required");
  native.record("pending", { repository_prerequisite: true });
  const configured = await page.request.post(manifest.base_url + setupPath, { headers: { Origin: manifest.base_url, "X-Sidera-CSRF": csrf.value },
    data: { repository_directory: manifest.repository_directory, expected_version: 0, planning_budget: config.planning_budget, planner_agent_ref: profile,
      verification_checks: [{ key: "owned-check", version: 1, argv: [config.settings.executable, "-B", "check.py"], cwd: ".", environment_ref: "env://owned-test",
        prepare_argvs: [], timeout_seconds: 10, success_exit_codes: [0], report_paths: [], required: true }] } });
  if (!configured.ok()) throw new Error("native_repository_prerequisite_refused");
  const setup = await native.get<Setup>(setupPath);
  expect(setup.workspace_id).toBe(manifest.workspace_id); expect(setup.version).toBe(1); expect(setup.planner_agent_ref).toEqual(profile);
  native.ids.planning_policy_id = setup.planning_policy_ref.policy_id;
  native.versions.defaults = defaults.version; native.versions.setup = setup.version; native.versions.planning_policy = setup.planning_policy_ref.version;
  return { ui_runtime: true, ui_profile: true, ui_task_tool_resource: true, ui_mcp_binding: true, ui_defaults: true, repository_ref: setup.repository_ref };
}

async function conversation(native: NativeFixture) {
  await native.page.goto(native.path("chat"));
  await native.page.getByRole("button", { name: "使用该 Profile 新建对话", exact: true }).click();
  await expect.poll(() => typeof native.ids.thread_id).toBe("string");
  await expect(native.page).toHaveURL(native.manifest.base_url + native.path(`chat/${native.id("thread_id")}`));
  const thread = await native.get<Product["schemas"]["ThreadView"]>(`/main-agent/v1/threads/${native.id("thread_id")}`);
  expect(thread.profileRef).toEqual(native.profile()); expect(thread.turns).toEqual([]); expect(thread.runId).toBeNull();
  return { current_profile_reviewed: true, no_run_before_turn: true };
}

async function configureTarget(native: NativeFixture) {
  const primary = { profile: native.profile(), operation: native.ids.configuration_operation_id, runtime: native.ids.runtime_id };
  native.configurationCapture = "target";
  try {
    await native.page.goto(native.path("agents/new"));
    await native.page.getByRole("textbox", { name: "名称", exact: true }).fill("Native transition target");
    await native.page.getByRole("combobox", { name: "运行环境", exact: true }).selectOption(native.id("runtime_id"));
    await native.page.getByRole("textbox", { name: "指令", exact: true }).fill("Preserve the original input and generation");
    await native.page.getByRole("spinbutton", { name: "并行安排上限", exact: true }).fill("4");
    await native.page.getByRole("button", { name: "保存 Profile", exact: true }).click();
    await expect.poll(() => typeof native.ids.target_profile_id).toBe("string");
    const created = native.ids.target_profile_create_ref as Product["schemas"]["ProfileRef"];
    await expect(native.page).toHaveURL(native.manifest.base_url + native.path(`agents/${created.profile_id}?view=settings`));
    await bindTaskTools(native, created, true);
    const view = await native.get<Product["schemas"]["ProfileView"]>(`/agents/v1/workspaces/${native.manifest.workspace_id}/profiles/${native.id("target_profile_id")}?revision=2`);
    expect(view.profile.ref).toEqual(native.ids.target_profile_ref);
    expect(view.profile.ref).not.toEqual(primary.profile);
    expect(view.profile.draft.runtime_ref).toEqual({ workspace_id: native.manifest.workspace_id, runtime_id: primary.runtime });
    expect(view.profile.draft.resource_refs).toEqual([native.ids.task_tool_resource_ref]);
    expect(native.profile()).toEqual(primary.profile); expect(native.ids.configuration_operation_id).toBe(primary.operation); expect(native.ids.runtime_id).toBe(primary.runtime);
    const defaults = await native.get<Product["schemas"]["DefaultsView"]>(`/product/v1/workspaces/${native.manifest.workspace_id}/defaults`);
    expect([defaults.main_profile_id, defaults.planner_profile_id, defaults.worker_profile_id]).toEqual([primary.profile.profile_id, primary.profile.profile_id, primary.profile.profile_id]);
    return { separate_target_profile: true, target_profile_ref: native.ids.target_profile_ref!, target_configuration_operation_id: native.ids.target_configuration_operation_id! };
  } finally { native.configurationCapture = "primary"; }
}

async function submitGoal(native: NativeFixture) {
  const { page } = native;
  await page.goto(native.path(`chat/${native.id("thread_id")}`));
  await page.getByRole("textbox", { name: "给 Main Agent 的消息" }).fill("Build the checked native result");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => typeof native.ids.turn_id).toBe("string");
  const turnPath = `/main-agent/v1/threads/${native.id("thread_id")}/turns/${native.id("turn_id")}`;
  await expect.poll(async () => (await native.get<Product["schemas"]["TurnView"]>(turnPath)).status, { timeout: 120_000 }).toBe("completed");
  const turn = await native.get<Product["schemas"]["TurnView"]>(turnPath);
  expect(turn.threadId).toBe(native.id("thread_id")); expect(turn.operationId).toBe(native.ids.turn_operation_id); expect(turn.profileRef).toEqual(native.profile());
  if (!turn.runId) throw new Error("native_original_chat_run_required");
  native.ids.chat_run_id = turn.runId;
  const receipt = turn.items.find(item => item.type === "toolCall" && item.data.receiptSource === "sidera.product.task-receipt.v1" && item.data.name === "sidera_submit_goal" && item.data.verified === true)?.data;
  if (!receipt || typeof receipt.rootTaskId !== "string" || typeof receipt.planningRequestId !== "string" || typeof receipt.commandId !== "string") throw new Error("native_verified_product_goal_receipt_required");
  native.ids.root_task_id = receipt.rootTaskId; native.ids.planning_request_id = receipt.planningRequestId; native.ids.goal_command_id = receipt.commandId;
  const link = page.locator(`.tool-result-card a[href="${native.path(`tasks/${native.id("root_task_id")}`)}"]`);
  await expect(link).toBeVisible(); await link.click();
  await expect(page).toHaveURL(native.manifest.base_url + native.path(`tasks/${native.id("root_task_id")}`));
  const result = await native.get<ResultOverview>(native.prefix() + `tasks/${native.id("root_task_id")}/results`);
  expect(result.task_status).not.toBe("succeeded");
  expect((await native.facts()).native.opens.filter(item => item.role === "worker")).toEqual([]);
  return { actual_goal_receipt: true, workers_before_confirmation: 0 };
}

async function proposalReady(native: NativeFixture) {
  const path = native.prefix() + `planning-requests/${native.id("planning_request_id")}`;
  await expect.poll(async () => (await native.get<Planning>(path)).status, { timeout: 120_000 }).toBe("proposal_ready");
  const planning = await native.get<Planning>(path);
  expect(planning.workspace_id).toBe(native.manifest.workspace_id); expect(planning.id).toBe(native.id("planning_request_id"));
  if (!planning.current_proposal_revision || !planning.current_proposal_content_hash) throw new Error("native_original_proposal_required");
  const proposal = await native.get<CP["schemas"]["PlanningProposalResponse"]>(path + `/proposals/${planning.current_proposal_revision}`);
  expect(proposal.proposal_schema_version).toBe(6); expect(proposal.content_hash).toBe(planning.current_proposal_content_hash);
  native.ids.proposal_id = proposal.id; native.ids.proposal_revision = proposal.revision; native.ids.proposal_content_hash = proposal.content_hash;
  native.versions.planning_request = planning.version;
  await native.page.goto(native.path(`planning/${planning.id}`));
  await expect(native.page.getByRole("button", { name: "确认并创建任务", exact: true })).toBeVisible();
  return { actual_proposal_schema: 6, current_proposal_revision: proposal.revision };
}

async function confirmPlan(native: NativeFixture) {
  const { page } = native, profile = native.profile();
  await page.goto(native.path(`planning/${native.id("planning_request_id")}`));
  await page.getByRole("combobox", { name: "one subtask owner", exact: true }).selectOption("human");
  for (const title of ["source", "hard-only", "integration"]) {
    await page.getByRole("combobox", { name: `${title} owner`, exact: true }).selectOption(JSON.stringify(["profile", profile.profile_id, profile.workspace_id, profile.revision]));
  }
  expect((await native.facts()).native.opens.filter(item => item.role === "worker")).toEqual([]);
  await page.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await expect.poll(() => typeof native.ids.confirmation_command_id).toBe("string");
  await expect.poll(async () => (await native.get<Planning>(
    native.prefix() + `planning-requests/${native.id("planning_request_id")}`
  )).status, { timeout: 120_000 }).toBe("materialized");
  const path = native.prefix() + `planning-requests/${native.id("planning_request_id")}/materialization`;
  await expect.poll(async () => (await native.get<Materialization>(path)).root_task_id).toBe(native.id("root_task_id"));
  const materialized = await native.get<Materialization>(path);
  expect(materialized.workspace_id).toBe(native.manifest.workspace_id); expect(materialized.proposal_revision_id).toBe(native.id("proposal_id"));
  expect(Object.keys(materialized.worker_item_mapping).sort()).toEqual(["hard-only", "integration", "source"]);
  native.ids.materialization_id = materialized.id; native.ids.plan_snapshot_id = materialized.plan_snapshot_id; native.ids.plan_generation = materialized.plan_generation;
  for (const [role, id] of Object.entries(materialized.worker_item_mapping)) native.ids[`${role.replace("-", "_")}_worker_item_id`] = id;
  const subtask = materialized.subtask_task_mapping["subtask-one"];
  if (!subtask) throw new Error("native_original_subtask_required");
  native.ids.subtask_id = subtask;
  const person = await native.get<CP["schemas"]["AgentBindingView"]>(native.prefix() + `assignments/subtask/${subtask}`);
  expect(person.assignee).toEqual({ kind: "human", id: native.manifest.principal_id }); expect(person.profile_ref).toBeNull();
  for (const id of Object.values(materialized.worker_item_mapping)) {
    const binding = await native.get<CP["schemas"]["AgentBindingView"]>(native.prefix() + `assignments/worker_item/${id}`);
    expect(binding.assignee).toEqual({ kind: "profile", id: profile.profile_id }); expect(binding.profile_ref).toEqual(profile);
  }
  return { explicit_subtask_human: true, explicit_worker_profile: true, materialization_id: materialized.id };
}

async function dependencyExecution(native: NativeFixture) {
  const { page } = native;
  await page.goto(native.path(`tasks/${native.id("root_task_id")}`));
  await expect(page.getByRole("tab", { name: "看板", exact: true })).toHaveAttribute("aria-selected", "true");
  const board = page.getByRole("region", { name: "子任务状态看板" });
  for (const status of ["需要处理", "执行中", "等待依赖", "下一步", "最近完成"]) await expect(board.getByRole("heading", { name: status, exact: true })).toBeVisible();
  await board.getByRole("button", { name: "打开子任务：one subtask", exact: true }).click();
  await expect(page.getByRole("region", { name: "Worker 看板" })).toBeVisible();
  await page.getByRole("tab", { name: "Graph", exact: true }).click();
  const edges = page.getByRole("group", { name: "Worker 关系" });
  await expect(edges.getByText("source → hard-only · dependency", { exact: true })).toBeVisible();
  await expect(edges.getByText("hard-only → integration · dependency", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "打开 Worker：source", exact: true }).click();
  const context = await native.waitForSourceContext(native.id("source_worker_item_id")), pin = currentNativeTracePin(context);
  if (!context.context || !context.binding) throw new Error("native_original_source_context_required");
  native.ids.source_execution_id = pin.execution_id; native.ids.source_run_id = pin.run_id; native.ids.source_session_id = context.context.session_id;
  native.ids.binding_version = context.context.binding_version; native.ids.plan_generation = context.context.plan_generation;
  native.ids.source_context_version = context.context.context_version;
  native.versions.context = context.context.context_version; native.versions.session = context.context.session_version; native.versions.worker_item = context.worker_item_version;
  expect(pin.profile_ref).toEqual(native.profile()); expect(context.binding.profile_ref).toEqual(native.profile());
  const facts = await native.facts();
  expect(Object.keys(facts.native.worker_facts)).toEqual(["source"]); expect(facts.native.original_run_id).toBe(pin.run_id);
  await expect(page.getByRole("heading", { name: "工作记录", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Agent Trace" })).toHaveCount(0);
  return { dependency_source_only: true, original_generation: context.context.plan_generation, original_binding: context.context.binding_version };
}

async function workerInput(native: NativeFixture) {
  const { page } = native;
  await page.goto(native.path(`worker-items/${native.id("source_worker_item_id")}`));
  native.loseInputResponse = true;
  const text = "Keep the original checked value and report this intervention";
  await page.getByRole("textbox", { name: "介入这个 Worker", exact: true }).fill(text);
  await page.getByRole("button", { name: "复核当前 context", exact: true }).click();
  await expect(page.getByRole("button", { name: "发送到当前 Session", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "发送到当前 Session", exact: true }).click();
  await expect.poll(() => typeof native.ids.input_command_id).toBe("string");
  await expect.poll(() => typeof native.ids.input_id).toBe("string");
  await page.getByRole("button", { name: "查询原始输入命令", exact: true }).click();
  const command = await native.get<CP["schemas"]["CommandStatusResponse"]>(native.prefix() + `commands/${native.id("input_command_id")}`);
  expect(command.status).toBe("applied"); expect(command.result?.aggregate_id).toBe(native.id("input_id"));
  const path = `/api/v1/gateway/inputs?workspace_id=${native.manifest.workspace_id}&execution_id=${native.id("source_execution_id")}&session_id=${native.id("source_session_id")}`;
  await expect.poll(async () => (await native.get<CP["schemas"]["ExecutionInputPageResponse"]>(path)).items.some(item => item.id === native.ids.input_id && typeof item.operation_id === "string"),
    { timeout: 120_000 }).toBe(true);
  const row = (await native.get<CP["schemas"]["ExecutionInputPageResponse"]>(path)).items.find(item => item.id === native.ids.input_id);
  if (!row?.operation_id) throw new Error("native_original_input_operation_required");
  expect(row.workspace_id).toBe(native.manifest.workspace_id); expect(row.execution_id).toBe(native.id("source_execution_id"));
  native.ids.input_operation_id = row.operation_id;
  await page.getByRole("button", { name: "读取原生投递状态", exact: true }).click();
  await expect.poll(async () => {
    try { await native.bridge("/release-prompt", { run_id: native.id("source_run_id"), input_operation_id: row.operation_id! }); return true; }
    catch { return false; }
  }, { timeout: 120_000 }).toBe(true);
  expect((await native.facts()).native.accepted_inputs[native.id("source_run_id")]).toBe(row.operation_id);
  await expect.poll(async () => (await native.get<CP["schemas"]["ExecutionInputPageResponse"]>(path)).items.find(item => item.id === native.ids.input_id)?.status,
    { timeout: 120_000 }).toBe("delivered");
  await page.getByRole("button", { name: "读取原生投递状态", exact: true }).click();
  await expect(page.getByLabel("原生操作状态")).toContainText("已投递");
  const inputs = (await native.facts()).native.inputs.filter(item => item.run_id === native.id("source_run_id"));
  expect(inputs).toHaveLength(2); expect(inputs[0]?.response_id).not.toBe(inputs[1]?.response_id);
  expect(native.mutations.filter(item => item.command_type === "SendExecutionInput")).toHaveLength(1);
  const after = await native.get<NativeContext>(native.prefix() + `worker-items/${native.id("source_worker_item_id")}/native-context`);
  expect(after.execution?.run_id).toBe(native.id("source_run_id")); expect(after.execution?.execution_id).toBe(native.id("source_execution_id"));
  return { actual_native_admission: true, same_run_delivery: true, lost_response_original_lookup: true,
    source_response_ids: inputs.map(item => item.response_id), intervention_text_length: text.length };
}

async function resultVerified(native: NativeFixture) {
  const path = native.prefix() + `tasks/${native.id("root_task_id")}/results`;
  await expect.poll(async () => (await native.get<ResultOverview>(path)).result_stage, { timeout: 120_000 }).toBe("awaiting_acceptance");
  const view = await native.get<ResultOverview>(path), candidate = view.current_candidate;
  expect(view.task_status).not.toBe("succeeded"); expect(view.acceptance).toBeNull();
  if (!candidate?.result_commit || !candidate.result_tree) throw new Error("native_real_captured_candidate_required");
  expect(view.criteria.every(item => item.verification_status === "passed")).toBe(true);
  const detail = await native.get<ResultDetail>(path + `/${candidate.candidate_id}`);
  const check = detail.verifications.find(item => item.check_key === "owned-check" && item.status === "passed" && item.exit_code === 0);
  if (!check) throw new Error("native_real_passed_owned_check_required");
  expect(check.actual_commit).toBe(candidate.result_commit); expect(check.actual_tree).toBe(candidate.result_tree); expect(check.candidate_id).toBe(candidate.candidate_id);
  native.ids.candidate_id = candidate.candidate_id; native.ids.candidate_hash = candidate.candidate_hash; native.ids.result_commit = candidate.result_commit;
  native.ids.result_tree = candidate.result_tree; native.ids.verification_id = check.verification_id;
  native.versions.task = view.task_version; native.versions.result = view.result_version;
  const facts = await native.facts();
  expect(Object.keys(facts.native.worker_facts)).toEqual(["source", "hard-only", "integration"]);
  expect(facts.native.physical_inputs).toHaveLength(2);
  for (const observed of facts.native.physical_inputs) {
    const code = await native.get<{ code_input_manifest: { execution_id: string; input_hash: string; selection_id: string; selection_hash: string } }>(native.prefix() + `executions/${observed.execution_id}/context-package?manifest_schema_version=3`);
    expect(code.code_input_manifest.execution_id).toBe(observed.execution_id); expect(code.code_input_manifest.input_hash).toBe(observed.input_hash);
    expect(code.code_input_manifest.selection_id).toBe(observed.selection_id); expect(code.code_input_manifest.selection_hash).toBe(observed.selection_hash);
  }
  await native.page.goto(native.path(`tasks/${native.id("root_task_id")}`));
  await native.page.getByRole("tab", { name: "成果", exact: true }).click();
  await expect(native.page.getByRole("region", { name: "成果验收" })).toContainText("owned-check");
  return { immutable_candidate_verified: true, physical_inputs: facts.native.physical_inputs as unknown as Json,
    repository_proof: native.repositoryProof(detail.candidate, detail.verifications) };
}

async function resultAccepted(native: NativeFixture) {
  const { page } = native, root = native.id("root_task_id");
  await page.goto(native.path(`tasks/${root}`));
  await page.getByRole("tab", { name: "成果", exact: true }).click();
  const view = await native.get<ResultOverview>(native.prefix() + `tasks/${root}/results`);
  expect(view.current_candidate?.candidate_id).toBe(native.id("candidate_id")); expect(view.apply_policy?.apply_on_accept).toBe(false);
  for (const criterion of view.criteria) await page.getByRole("combobox", { name: `${criterion.text} 的决定`, exact: true }).selectOption("accept_passed");
  await page.getByRole("button", { name: "接受成果", exact: true }).click();
  await expect.poll(() => typeof native.ids.acceptance_command_id).toBe("string");
  const path = native.prefix() + `tasks/${root}/results`;
  await expect.poll(async () => (await native.get<ResultOverview>(path)).task_status, { timeout: 120_000 }).toBe("succeeded");
  const finished = await native.get<ResultOverview>(path);
  expect(finished.acceptance?.candidate_id).toBe(native.id("candidate_id")); expect(finished.acceptance?.candidate_hash).toBe(native.ids.candidate_hash);
  expect(finished.acceptance?.command_id).toBe(native.id("acceptance_command_id")); expect(finished.apply_policy?.apply_on_accept).toBe(false);
  const detail = await native.get<ResultDetail>(path + `/${native.id("candidate_id")}`);
  expect(detail.apply_operations).toEqual([]);
  await expect(page.getByRole("button", { name: "接受成果", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "查询原始验收", exact: true }).click();
  return { actual_finalization_succeeded: true, apply_operations: 0, repository_proof: native.repositoryProof(detail.candidate, detail.verifications) };
}

function assertOriginalTransition(native: NativeFixture, proposal: NativeProposal, transition?: NativeTransition) {
  const target = native.ids.target_profile_ref;
  expect(proposal.id).toBe(native.id("handoff_proposal_id")); expect(proposal.root_task_id).toBe(native.id("root_task_id"));
  expect(proposal.proposal_schema_version).toBe(2);
  for (const value of transition ? [proposal, transition] : [proposal]) {
    expect(value.workspace_id).toBe(native.manifest.workspace_id); expect(value.worker_item_id).toBe(native.id("source_worker_item_id"));
    expect(value.source_execution_id).toBe(native.id("source_execution_id")); expect(value.source_run_id).toBe(native.id("source_run_id"));
    expect(value.source_session_id).toBe(native.id("source_session_id")); expect(value.source_profile_ref).toEqual(native.profile());
    expect(value.source_binding_version).toBe(native.ids.binding_version); expect(value.plan_base.generation).toBe(native.ids.plan_generation);
    expect(value.context.source_context_version).toBe(native.ids.source_context_version);
    expect(value.target_profile_ref).toEqual(target); expect(value.target_assignee).toEqual({ kind: "profile", id: native.id("target_profile_id") });
  }
  if (transition) {
    expect(transition.id).toBe(native.id("handoff_transition_id")); expect(transition.proposal_id).toBe(proposal.id); expect(transition.handoff_schema_version).toBe(2);
    expect(transition.failure_code).toBe("source_stop_unconfirmed"); expect(transition.target_execution_id).toBeNull(); expect(transition.target_run_id).toBeNull();
    expect(transition.available_recovery_strategies).toEqual([]);
  }
}

async function focusedTransition(native: NativeFixture, originalOnly = false) {
  expect(native.readOnly).toBe(originalOnly);
  const { page } = native, root = native.id("root_task_id"), source = native.id("source_worker_item_id"), run = native.id("source_run_id");
  const screenshots: Json[] = [];
  const target = native.ids.target_profile_ref as unknown as Product["schemas"]["ProfileRef"];
  expect(target.workspace_id).toBe(native.manifest.workspace_id); expect(target.profile_id).not.toBe(native.profile().profile_id);
  if (!originalOnly) {
    await native.bridge("/mark-exit-unknown", { run_id: run });
    await expect.poll(async () => (await native.facts()).functional.transition_source_unknown_ready, { timeout: 120_000 }).toMatchObject({ run_id: run });
    await page.goto(native.path(`worker-items/${source}`));
    const ownership = page.getByRole("region", { name: "归属设置", exact: true });
    await ownership.getByRole("combobox", { name: "设置归属", exact: true }).selectOption(JSON.stringify(["profile", target.profile_id, target.workspace_id, target.revision]));
    const startHandoff = ownership.getByRole("button", { name: "开始原生交接", exact: true });
    const rereview = ownership.getByRole("button", { name: "重新读取并复核", exact: true });
    await expect.poll(async () => await startHandoff.isVisible() || await rereview.isVisible()).toBe(true);
    if (await rereview.isVisible()) await rereview.click();
    await expect(startHandoff).toBeEnabled();
    await startHandoff.click();
    const dialog = page.getByRole("dialog", { name: "原生 Profile 交接", exact: true });
    await expect(dialog.getByLabel("固定来源事实")).toContainText(run);
    await dialog.getByRole("textbox", { name: "交接原因", exact: true }).fill("Explicit original source reassignment");
    await dialog.getByRole("textbox", { name: "交接指令", exact: true }).fill("Preserve the original input and generation");
    await dialog.getByRole("button", { name: "准备原生交接", exact: true }).click();
    await expect.poll(() => typeof native.ids.handoff_prepare_command_id).toBe("string");
    await expect.poll(() => typeof native.ids.handoff_proposal_id).toBe("string");
    const proposalPath = native.prefix() + `handoff-proposals/${native.id("handoff_proposal_id")}`;
    const proposal = await native.get<NativeProposal>(proposalPath);
    assertOriginalTransition(native, proposal);
    const review = page.getByRole("dialog", { name: "复核原生交接方案", exact: true });
    await expect(review.getByLabel("固定来源事实")).toContainText(run);
    await expect(review.getByLabel("固定来源事实")).toContainText(`V${native.ids.binding_version}`);
    await expect(review.locator(".handoff-review-participants")).toContainText(target.profile_id);
    await expect(review.locator(".handoff-dialog__facts").last()).toContainText(`Generation ${native.ids.plan_generation}`);
    await review.getByRole("button", { name: "确认交接", exact: true }).click();
    await expect.poll(() => typeof native.ids.handoff_confirm_command_id).toBe("string");
    await expect.poll(() => typeof native.ids.handoff_transition_id).toBe("string");
  } else {
    for (const key of ["handoff_prepare_command_id", "handoff_proposal_id",
      "handoff_confirm_command_id", "handoff_transition_id"]) native.id(key);
    await page.goto(native.path(`worker-items/${source}`));
    await page.getByRole("button", { name: "查看交接记录", exact: true }).click();
  }
  const proposalPath = native.prefix() + `handoff-proposals/${native.id("handoff_proposal_id")}`;
  const proposal = await native.get<NativeProposal>(proposalPath);
  const transitionPath = native.prefix() + `handoff-transitions/${native.id("handoff_transition_id")}`;
  await expect.poll(async () => (await native.get<NativeTransition>(transitionPath)).failure_code, { timeout: 120_000 }).toBe("source_stop_unconfirmed");
  const blocked = await native.get<NativeTransition>(transitionPath);
  assertOriginalTransition(native, proposal, blocked);
  expect((await native.facts()).native.opens.filter(item => item.profile_ref.profile_id === target.profile_id)).toEqual([]);
  const after = await native.get<NativeContext>(native.prefix() + `worker-items/${source}/native-context`);
  expect(currentNativeTracePin(after).run_id).toBe(run); expect(after.binding?.version).toBe(native.ids.binding_version);
  expect(after.context?.plan_generation).toBe(native.ids.plan_generation);
  const progressDialog = page.getByRole("dialog", { name: "原生交接进度", exact: true });
  const progress = progressDialog.getByRole("region", { name: "原生交接进度", exact: true });
  await expect(progressDialog).toBeVisible();
  await progress.getByRole("button", { name: "重新读取原生进度", exact: true }).click();
  await expect(progress).toContainText("source_stop_unconfirmed"); await expect(progress).toContainText(run);
  await expect(progress.getByRole("button")).toHaveCount(2);
  screenshots.push(await native.screenshot("focused-progress"));
  await progress.getByRole("button", { name: "关闭原生交接进度", exact: true }).click();
  const navigationStart = native.mutations.length;
  await page.reload();
  await page.getByRole("button", { name: "查看交接记录", exact: true }).click();
  await expect(progress).toContainText(blocked.id);
  expect((await native.get<NativeTransition>(transitionPath)).id).toBe(blocked.id);
  await progress.getByRole("button", { name: "关闭原生交接进度", exact: true }).click();
  const overview = (await native.get<CP["schemas"]["TaskConsoleOverviewProjectionResponse"][]>(native.prefix() + "task-overviews"))
    .find(item => item.root_task_id === root);
  expect(overview).toBeDefined();
  expect(overview?.handoff_running_count).toBe(1);
  expect(overview?.handoff_blocked_count).toBe(0);
  expect(overview?.attention_items.filter(item => item.kind === "handoff"
    && item.worker_item_id === source && item.correlation_id === blocked.id)).toEqual([]);
  const workspace = await native.get<CP["schemas"]["WorkerWorkspaceProjectionResponse"]>(native.prefix() + `worker-items/${source}/workspace`);
  expect(workspace.active_handoff).toMatchObject({ transition_id: blocked.id,
    proposal_id: proposal.id, status: "running", phase: "stop_source",
    writer_state: "source_stop_requested", failure_code: "source_stop_unconfirmed",
    attention_required: false, available_recovery_strategies: [],
    source_execution_id: native.id("source_execution_id"),
    source_session_id: native.id("source_session_id"), target_execution_id: null,
    target_session_id: null });
  const threadsPath = native.prefix() + `notification-threads?category=action&root_task_id=${root}&limit=100`;
  const sourcePath = native.path(`worker-items/${source}`), sourceOrigin = new URL(native.manifest.base_url).origin;
  const threads = (await native.get<CP["schemas"]["NotificationThreadPageResponse"]>(threadsPath)).items.filter(item => {
    const link = new URL(item.deep_link, native.manifest.base_url);
    return item.workspace_id === native.manifest.workspace_id && item.root_task_id === root && item.worker_item_id === source
      && item.action === "检查 WorkerItem 切换" && link.origin === sourceOrigin && link.pathname === sourcePath;
  });
  expect(threads).toHaveLength(1);
  const thread = threads[0];
  if (!thread) throw new Error("native_existing_worker_activity_thread_required");
  const history = await native.get<CP["schemas"]["NotificationThreadDetailResponse"]>(native.prefix() + `notification-threads/${thread.id}?limit=100`);
  expect(history.thread.workspace_id).toBe(thread.workspace_id); expect(history.thread.root_task_id).toBe(thread.root_task_id);
  expect(history.thread.worker_item_id).toBe(thread.worker_item_id); expect(history.thread.id).toBe(thread.id);
  const notifications = history.facts.items.filter(item => {
    const link = new URL(item.deep_link, native.manifest.base_url);
    return item.kind === "handoff_attention" && item.thread_id === history.thread.id && item.root_task_id === root
      && item.worker_item_id === source && item.source_projection_type === "worker_item_activity" && item.source_resource_id === source
      && link.origin === sourceOrigin && link.pathname === sourcePath;
  });
  expect(notifications).toHaveLength(1);
  const notification = notifications[0];
  if (!notification) throw new Error("native_existing_worker_activity_fact_required");
  expect(notification.correlation_id).not.toBe(blocked.id);
  expect(history.facts.items.filter(item => item.correlation_id === blocked.id)).toEqual([]);
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1366, height: 768 }]) {
    await page.setViewportSize(viewport);
    await page.goto(native.path(`tasks/${root}`));
    await page.getByRole("tab", { name: "看板", exact: true }).click();
    const board = page.getByRole("region", { name: "子任务状态看板", exact: true });
    await expect(board).toBeVisible();
    screenshots.push(await native.screenshot("focused-waiting-task"));
    await board.getByRole("button", { name: "打开子任务：one subtask", exact: true }).click();
    await expect(page).toHaveURL(native.manifest.base_url + native.path(`tasks/${root}/subtasks/subtask-one`));
    await page.getByRole("button", { name: "打开 Worker：source", exact: true }).click();
    await expect(page).toHaveURL(native.manifest.base_url + native.path(`worker-items/${source}`));
    await expect(page.getByRole("heading", { name: "工作记录", exact: true })).toBeVisible();
    const inlineProgress = page.getByRole("region", { name: "原生交接进度", exact: true });
    await expect(inlineProgress).toContainText(run);
    await expect(inlineProgress).toContainText("等待源 Agent 停止证据");
    screenshots.push(await native.screenshot("focused-worker"));
    await page.getByRole("button", { name: "查看交接记录", exact: true }).click();
    await expect(progress).toContainText(blocked.id);
    screenshots.push(await native.screenshot("focused-progress"));
    await progress.getByRole("button", { name: "关闭原生交接进度", exact: true }).click();
    await page.getByRole("button", { name: "查看当前 Agent Trace", exact: true }).click();
    const traceDialog = page.getByRole("dialog", { name: "Agent Trace", exact: true });
    await traceDialog.getByText("原始执行", { exact: true }).click();
    await expect(traceDialog.getByText(run, { exact: true })).toBeVisible();
    await expect(traceDialog.getByText(native.id("source_execution_id"), { exact: true })).toBeVisible();
    await expect(traceDialog.getByRole("region", { name: "原始 Run 记录", exact: true })).toBeVisible();
    screenshots.push(await native.screenshot("focused-source-trace"));
    await traceDialog.getByRole("button", { name: "关闭 Agent Trace", exact: true }).click();
    await page.goto(native.path("inbox")); await page.getByRole("tab", { name: /^需要处理/ }).click();
    const threadToggle = page.locator(`button[aria-controls="notification-thread-history-${thread.id}"]`);
    await expect(threadToggle).toHaveCount(1);
    await expect(threadToggle).toHaveAccessibleName(`展开${thread.title}`);
    await threadToggle.click();
    const threadHistory = page.locator(`#notification-thread-history-${thread.id}`).getByRole("region", { name: `${thread.title}的线程历史` });
    await expect(threadHistory).toContainText(notification.summary);
    await threadHistory.getByRole("button", { name: "打开 WorkerItem", exact: true }).click();
    await expect(page).toHaveURL(native.manifest.base_url + native.path(`worker-items/${source}`));
  }
  expect(native.mutations.slice(navigationStart)).toEqual([]);
  await page.setViewportSize({ width: 1440, height: 900 });
  await native.bridge("/confirm-exit", { run_id: run });
  const known = await native.facts(); expect(known.native.inspections[run]?.process_state).toBe("gone");
  expect(known.native.opens.filter(item => item.profile_ref.profile_id === target.profile_id)).toEqual([]);
  for (const [key, aggregate] of [["handoff_prepare_command_id", "handoff_proposal_id"], ["handoff_confirm_command_id", "handoff_transition_id"]]) {
    const command = await native.get<CP["schemas"]["CommandStatusResponse"]>(native.prefix() + `commands/${native.id(key!)}`);
    expect(command.command_id).toBe(native.id(key!)); expect(command.status).toBe("applied"); expect(command.result?.aggregate_id).toBe(native.id(aggregate!));
  }
  const observed = await native.get<NativeTransition>(transitionPath);
  assertOriginalTransition(native, await native.get<NativeProposal>(proposalPath), observed);
  const trace = await native.get<CP["schemas"]["NativeWorkerTraceView"]>(native.prefix() + `worker-items/${source}/executions/${native.id("source_execution_id")}/native-trace`);
  expect(exactNativeTrace(trace, { workspace_id: native.manifest.workspace_id, worker_item_id: source,
    execution_id: native.id("source_execution_id"), run_id: run, profile_ref: native.profile() })).toBe(true);
  if (originalOnly) expect(native.mutations).toEqual([]);
  else {
    expect(native.mutations.filter(item => item.command_type === "PrepareProfileHandoff")).toHaveLength(1);
    expect(native.mutations.filter(item => item.command_type === "ConfirmProfileHandoff")).toHaveLength(1);
  }
  const facts: Record<string, Json> = { focused_transition: true, original_source_blocked: false, original_source_stop_waiting: true, target_profile_opens: 0,
    original_only_resume: originalOnly, read_only_product_mutations: native.mutations.length,
    known_inspection: "gone", canonical_transition_status: observed.status, canonical_failure_code: observed.failure_code,
    canonical_converged: observed.status === "converged", available_recovery_strategies: observed.available_recovery_strategies,
    attention_key: null, transition_attention_required: false, transition_notification_observed: false,
    notification_scope: "existing_worker_activity", notification_correlation_id: notification.correlation_id,
    notification_thread_id: thread.id, notification_fact_id: notification.id,
    normal_client_history: true, fresh_client_history_discovery: "not_demonstrated", screenshots };
  return { ...facts, focused_receipt_path: native.recordFocusedTransition(facts) };
}

if (scenario === "transition" && process.env.SIDERA_NATIVE_READ_ONLY === "1")
  test("native original-only focused transition resume", async ({ native }) => {
    if (!native.readOnly || process.env.SIDERA_NATIVE_RESUME !== "1"
        || process.env.SIDERA_NATIVE_STAGE !== "dependency_execution") {
      throw new Error("native_original_transition_resume_refused");
    }
    const previous = nativeRecords(native.manifest)
      .filter(record => record.step === "dependency_execution").at(-1);
    if (!previous || previous.status !== "failed") {
      throw new Error("native_original_failed_transition_required");
    }
    for (const key of ["handoff_prepare_command_id", "handoff_proposal_id",
      "handoff_confirm_command_id", "handoff_transition_id"]) native.id(key);
    for (const [key, value] of Object.entries(previous.original_ids)) {
      expect(native.ids[key]).toEqual(value);
    }
    await native.authorize();
    expect(await native.observe(previous)).toBe(true);
    native.stage = "dependency_execution";
    native.stepId = previous.step_id;
    native.versions = { ...previous.versions };
    native.record("pending", { resume: true, original_only_resume: true });
    try {
      const observed = await focusedTransition(native, true);
      expect(native.mutations).toEqual([]);
      const proof = native.record("pending", { ...observed, canonical: false });
      expect(await native.observe(proof)).toBe(true);
      native.record("passed", { ...observed, canonical: true });
    } catch (error) {
      native.record("failed", { reason: "native_original_transition_assertion_failed" });
      throw error;
    }
  });
else if (scenario === "main") test("native product composed journey", async ({ native }) => {
  await native.authorize();
  await native.step("configuration", () => configure(native));
  await native.step("conversation", () => conversation(native));
  await native.step("goal_submitted", () => submitGoal(native));
  await native.step("proposal_ready", () => proposalReady(native));
  await native.step("plan_confirmed", () => confirmPlan(native));
  await native.step("dependency_execution", () => dependencyExecution(native));
  await native.step("worker_input", () => workerInput(native));
  await native.step("result_verified", () => resultVerified(native));
  await native.step("result_accepted", () => resultAccepted(native));
  await native.step("navigation_history", () => assertNativeNavigation(native));
});
else test("native product focused original transition", async ({ native }) => {
  await native.authorize();
  await native.step("configuration", async () => ({ ...await configure(native), ...await configureTarget(native) }));
  await native.step("conversation", () => conversation(native));
  await native.step("goal_submitted", () => submitGoal(native));
  await native.step("proposal_ready", () => proposalReady(native));
  await native.step("plan_confirmed", () => confirmPlan(native));
  await native.step("dependency_execution", async () => ({ ...await dependencyExecution(native), ...await focusedTransition(native) }));
});
