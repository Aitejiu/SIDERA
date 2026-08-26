import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { ContextPackagePage } from "./ContextPackagePage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const PACKAGE_ID = "70000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const PAGE_ID = "90000000-0000-0000-0000-000000000001";
const REVISION_ID = "91000000-0000-0000-0000-000000000001";
const WORK_CONTEXT_REVISION_ID = "92000000-0000-0000-0000-000000000001";

const packageManifest = {
  package_id: PACKAGE_ID,
  workspace_id: WORKSPACE_ID,
  purpose: "execution",
  consumer_type: "worker",
  consumer_id: EXECUTION_ID,
  worker_item_id: WORKER_ITEM_ID,
  work_context_revision_id: WORK_CONTEXT_REVISION_ID,
  work_context_revision_number: 7,
  work_context_content_hash: "c".repeat(64),
  context_version: 4,
  control_plane_fact_snapshot: {
    task_status: "running",
    worker_item_key: "callback-implementer",
  },
  knowledge_refs: [{
    page_id: PAGE_ID,
    revision_id: REVISION_ID,
    revision_hash: "a".repeat(64),
    section_key: "retry-policy",
    section_hash: "b".repeat(64),
    source_ids: ["93000000-0000-0000-0000-000000000001"],
    selection_reason: "work_context",
    trust_level: "workspace_confirmed",
    ordinal: 0,
  }],
  artifact_refs: [{ ref_id: "94000000-0000-0000-0000-000000000001", ref_type: "test-report", ordinal: 0 }],
  evidence_refs: [{ ref_id: "95000000-0000-0000-0000-000000000001", ref_type: "acceptance", ordinal: 0 }],
  budget: {
    profile_id: "96000000-0000-0000-0000-000000000001",
    profile_version: 2,
    max_sections: 8,
    max_chars: 12000,
    max_input_tokens: 3000,
    actual_sections: 1,
    actual_chars: 640,
    actual_input_tokens: 180,
  },
  expires_at: "2026-08-27T08:00:00Z",
  idempotency_key: "context-package-key",
  package_hash: "d".repeat(64),
  status: "active",
  created_at: "2026-08-26T08:00:00Z",
  materialized_at: null,
  revoked_at: null,
  revocation_reason: null,
  revocation_scope: null,
  assembly: {
    request_id: "97000000-0000-0000-0000-000000000001",
    status: "ready",
    attempt: 1,
    next_attempt_at: null,
    error_code: null,
    request_fingerprint: "fingerprint",
    version: 1,
    created_at: "2026-08-26T08:00:00Z",
    updated_at: "2026-08-26T08:01:00Z",
  },
  acknowledgements: [{
    package_id: PACKAGE_ID,
    consumer_type: "worker",
    consumer_id: EXECUTION_ID,
    context_version: 4,
    package_hash: "d".repeat(64),
    adapter_module_id: "98000000-0000-0000-0000-000000000001",
    adapter_config_version: 3,
    acknowledged_at: "2026-08-26T08:02:00Z",
  }],
  materialization_audits: [],
};

const exactRevision = {
  revision_id: REVISION_ID,
  page_id: PAGE_ID,
  workspace_id: WORKSPACE_ID,
  revision_number: 3,
  title: "支付重试手册",
  summary: "回调重试的稳定规则。",
  tags: ["payment"],
  content_hash: "e".repeat(64),
  change_reason: "初始导入",
  created_by: "aitejiu",
  created_at: "2026-08-25T08:00:00Z",
  accepted_candidate_id: null,
  sections: [],
  relations: [],
  source_refs: [],
  sources: [],
};

const materialization = {
  schema_version: 1,
  metadata: {
    package_id: PACKAGE_ID,
    package_hash: "d".repeat(64),
    purpose: "execution",
    consumer_type: "worker",
    consumer_id: EXECUTION_ID,
    context_version: 4,
    expires_at: "2026-08-27T08:00:00Z",
  },
  system_constraints: {
    objective: "保留回调上下文",
    constraints: [{ key: "scope", text: "只影响当前 WorkerItem", ordinal: 0 }],
    acceptance_criteria: [{ key: "tests", text: "测试通过", ordinal: 0 }],
  },
  work_context: {
    revision_id: WORK_CONTEXT_REVISION_ID,
    revision_number: 7,
    content_hash: "c".repeat(64),
    snapshot_schema_version: 1,
    missing_fact_codes: [],
    requested_outputs: [],
    accepted_decisions: [],
    clarification_decisions: [],
    unresolved_questions: [],
    checkpoint_refs: [],
  },
  control_plane_facts: { task_status: "running" },
  project_knowledge: [{
    page_id: PAGE_ID,
    revision_id: REVISION_ID,
    revision_hash: "a".repeat(64),
    section_key: "retry-policy",
    section_hash: "b".repeat(64),
    body_markdown: "## 物化正文\n\n这里是固定 revision 的执行规则。",
    sources: [{
      label: "支付规范",
      availability: "available",
      digest: "f".repeat(64),
      git_revision: null,
      last_checked_at: "2026-08-25T08:00:00Z",
      metadata: {},
      safe_reason_code: null,
    }],
    trust: "workspace_confirmed",
    ordinal: 0,
    section_byte_count: 640,
  }],
  artifact_and_evidence_refs: [],
  total_byte_count: 640,
  input_token_count: 180,
};

function installHandlers(manifest = packageManifest, onMaterialize?: () => void) {
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/context-packages/${PACKAGE_ID}`, () => HttpResponse.json(manifest)),
    http.get(`/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages/${PAGE_ID}/revisions/${REVISION_ID}`, () => HttpResponse.json(exactRevision)),
    http.post(`/api/v1/workspaces/${WORKSPACE_ID}/context-packages/${PACKAGE_ID}/materialize`, () => {
      onMaterialize?.();
      return HttpResponse.json(materialization, { headers: { "Cache-Control": "no-store" } });
    }),
  );
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context-packages/${PACKAGE_ID}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/context-packages/:packageId" element={<ContextPackagePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ContextPackagePage", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("loads the body only after an explicit owner audit and keeps the manifest exact", async () => {
    let materializeCalls = 0;
    installHandlers(packageManifest, () => {
      materializeCalls += 1;
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "上下文包" })).toBeVisible();
    expect(await screen.findByText("支付重试手册")).toBeVisible();
    expect(screen.getByText("WorkContext R7")).toBeVisible();
    expect(screen.getByText("Package active")).toBeVisible();
    expect(screen.getByText("retry-policy")).toBeVisible();
    expect(screen.getByText("workspace_confirmed")).toBeVisible();
    expect(screen.queryByText("物化正文")).not.toBeInTheDocument();
    expect(materializeCalls).toBe(0);

    await user.click(screen.getByRole("button", { name: "查看物化内容" }));

    expect(await screen.findByText("物化正文")).toBeVisible();
    expect(screen.getByText("支付规范")).toBeVisible();
    expect(materializeCalls).toBe(1);
    expect(screen.getByText("180 input tokens")).toBeVisible();
  });

  it("allows owner audit for expired packages but blocks all-materialization revocation", async () => {
    const user = userEvent.setup();
    installHandlers({
      ...packageManifest,
      expires_at: "2026-08-25T08:00:00Z",
      status: "revoked",
      revoked_at: "2026-08-26T08:00:00Z",
      revocation_reason: "replacement",
      revocation_scope: "all_materialization",
    } as unknown as typeof packageManifest);
    renderPage();

    expect(await screen.findByText("已撤销")).toBeVisible();
    expect(screen.getByText("已禁止物化")).toBeVisible();
    expect(screen.getByRole("button", { name: "查看物化内容" })).toBeDisabled();
    expect(within(screen.getByRole("region", { name: "上下文包状态" })).getByText(/all_materialization/)).toBeVisible();
    expect(screen.queryByText("物化正文")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "刷新上下文包" }));
  });

  it("keeps natural expiry and new-start revocation available for historical audit", async () => {
    installHandlers({
      ...packageManifest,
      expires_at: "2026-08-25T08:00:00Z",
      status: "active",
    });
    const expiredView = renderPage();
    expect(await expiredView.findByRole("button", { name: "查看物化内容" })).toBeEnabled();
    expiredView.unmount();

    installHandlers({
      ...packageManifest,
      status: "revoked",
      revoked_at: "2026-08-26T08:00:00Z",
      revocation_reason: "replacement",
      revocation_scope: "new_starts",
    } as unknown as typeof packageManifest);
    renderPage();
    expect(await screen.findByRole("button", { name: "查看物化内容" })).toBeEnabled();
  });
});
