import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { KnowledgeImportPage } from "./KnowledgeImportPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const COMMAND_ID = "60000000-0000-0000-0000-000000000001";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/knowledge/import`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/knowledge/import" element={<KnowledgeImportPage />} />
          <Route
            path="/workspaces/:workspaceId/knowledge/candidates/:candidateId"
            element={<div data-testid="candidate-route">候选详情已打开</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeImportPage", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("reads Markdown as safe plaintext and submits a pending candidate", async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/knowledge-api/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ command_id: COMMAND_ID, status: "pending" });
      }),
    );
    renderPage();

    const file = new File(
      ["# Import guide\n\n<script>alert(1)</script>"],
      "guide.md",
      { type: "text/markdown" },
    );
    const input = screen.getByLabelText("Markdown 文件", { selector: "input[type=file]" });
    expect(input).toHaveAttribute("accept", ".md,.markdown,text/markdown");
    await user.upload(input, file);

    expect(await screen.findByText("guide.md")).toBeVisible();
    expect(screen.getByLabelText("标题")).toHaveValue("Import guide");
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeVisible();
    expect(screen.getByText(/SHA-256/)).toBeVisible();

    await user.clear(screen.getByLabelText("标题"));
    await user.type(screen.getByLabelText("标题"), "Imported runtime guide");
    await user.type(screen.getByLabelText("标签"), "runtime, r5");
    await user.click(screen.getByRole("button", { name: "提交待审核候选" }));

    expect(await screen.findByTestId("candidate-route")).toBeVisible();
    expect(requestBody?.command_type).toBe("SubmitKnowledgeCandidate");
    const payload = requestBody?.payload as Record<string, unknown>;
    expect(payload.title).toBe("Imported runtime guide");
    expect(payload.operation).toBe("create_page");
    expect(payload.tags).toEqual(["runtime", "r5"]);
    expect((payload.sections as Array<Record<string, unknown>>)[0]?.body_markdown).toContain(
      "<script>alert(1)</script>",
    );
  });

  it("rejects a file over the 1 MiB hard limit before reading it", async () => {
    const user = userEvent.setup();
    renderPage();

    const oversized = new File([new Uint8Array(1024 * 1024 + 1)], "large.md", {
      type: "text/markdown",
    });
    await user.upload(screen.getByLabelText("Markdown 文件", { selector: "input[type=file]" }), oversized);

    expect(await screen.findByRole("alert")).toHaveTextContent("不能超过 1 MiB");
    expect(screen.getByRole("button", { name: "提交待审核候选" })).toBeDisabled();
  });

  it("reports invalid UTF-8 explicitly", async () => {
    const user = userEvent.setup();
    renderPage();

    const invalid = new File([new Uint8Array([0xff, 0xfe, 0xfa])], "broken.md", {
      type: "text/markdown",
    });
    await user.upload(screen.getByLabelText("Markdown 文件", { selector: "input[type=file]" }), invalid);

    expect(await screen.findByRole("alert")).toHaveTextContent("不是有效的 UTF-8 Markdown 文件");
    expect(screen.getByRole("button", { name: "提交待审核候选" })).toBeDisabled();
  });
});
