import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import {
  buildSubmitKnowledgeCandidateCommand,
  getKnowledgeCommandStatus,
  getKnowledgePage,
  knowledgePagesQueryKey,
  listKnowledgePages,
  searchKnowledge,
  submitKnowledgeCandidate,
} from "./api";
import type { SubmitCandidatePayload } from "./types";

const workspaceId = "20000000-0000-0000-0000-000000000001";
const pageId = "30000000-0000-0000-0000-000000000001";
const revisionId = "40000000-0000-0000-0000-000000000001";
const candidateId = "50000000-0000-0000-0000-000000000001";
const commandId = "60000000-0000-0000-0000-000000000001";

const candidatePayload: SubmitCandidatePayload = {
  candidate_schema_version: 1,
  candidate_id: candidateId,
  operation: "revise_page",
  target_page_id: pageId,
  expected_revision_id: revisionId,
  expected_revision_hash: "a".repeat(64),
  title: "Repository guide",
  slug: "repository-guide",
  summary: "Updated guide",
  tags: ["architecture"],
  sections: [
    {
      section_key: "overview",
      heading: "Overview",
      summary: "",
      body_markdown: "Reference body",
      ordinal: 0,
    },
  ],
  relations: [],
  sources: [],
};

describe("Knowledge API client", () => {
  it("uses the knowledge proxy and keeps the service query key separate", async () => {
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${workspaceId}/knowledge/pages`,
        ({ request }) => {
          const url = new URL(request.url);
          expect(url.searchParams.get("include_archived")).toBe("false");
          expect(url.searchParams.get("limit")).toBe("20");
          return HttpResponse.json({ items: [], next_cursor: null });
        },
      ),
      http.get(
        `/knowledge-api/api/v1/workspaces/${workspaceId}/knowledge/search`,
        ({ request }) => {
          const url = new URL(request.url);
          expect(url.searchParams.get("q")).toBe("context");
          expect(url.searchParams.getAll("tags")).toEqual(["r5"]);
          return HttpResponse.json({ items: [], next_cursor: null });
        },
      ),
    );

    await expect(listKnowledgePages(workspaceId, { limit: 20 })).resolves.toEqual({
      items: [],
      next_cursor: null,
    });
    await expect(searchKnowledge(workspaceId, "context", { tags: ["r5"] })).resolves.toEqual({
      items: [],
      next_cursor: null,
    });
    expect(knowledgePagesQueryKey(workspaceId)).toEqual([
      "knowledge",
      workspaceId,
      "pages",
    ]);
  });

  it("reads page and exact revision through distinct knowledge routes", async () => {
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${workspaceId}/knowledge/pages/${pageId}`,
        () => HttpResponse.json({ page_id: pageId }),
      ),
      http.get(
        `/knowledge-api/api/v1/workspaces/${workspaceId}/knowledge/pages/${pageId}/revisions/${revisionId}`,
        () => HttpResponse.json({ revision_id: revisionId }),
      ),
    );

    await expect(getKnowledgePage(workspaceId, pageId)).resolves.toEqual({ page_id: pageId });
    await expect(getKnowledgePage(workspaceId, pageId, revisionId)).resolves.toEqual({
      revision_id: revisionId,
    });
  });

  it("normalizes a knowledge HTTP failure without replacing its stable code", async () => {
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${workspaceId}/knowledge/pages`,
        () =>
          HttpResponse.json(
            { error: { code: "knowledge_unavailable", message: "offline", details: {} } },
            { status: 503 },
          ),
      ),
    );

    await expect(listKnowledgePages(workspaceId)).rejects.toMatchObject({
      status: 503,
      code: "knowledge_unavailable",
    });
  });

  it("builds and submits a review-only candidate command", async () => {
    const command = buildSubmitKnowledgeCandidateCommand({
      workspaceId,
      payload: candidatePayload,
    });
    expect(command.command_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(command.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
    expect(command.correlation_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(command.actor).toEqual({ type: "local_owner", id: "aitejiu" });
    expect(command.target_id).toBe(candidateId);

    server.use(
      http.post("/knowledge-api/api/v1/commands", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body.command_type).toBe("SubmitKnowledgeCandidate");
        expect(body.workspace_id).toBe(workspaceId);
        expect((body.payload as Record<string, unknown>).candidate_id).toBe(candidateId);
        return HttpResponse.json({ command_id: command.command_id, status: "pending" });
      }),
    );

    await expect(submitKnowledgeCandidate(command)).resolves.toEqual({
      command_id: command.command_id,
      status: "pending",
    });
  });

  it("exposes command status recovery after a write has no observed result", async () => {
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${workspaceId}/commands/${commandId}`,
        () => HttpResponse.json({ command_id: commandId, status: "pending" }),
      ),
    );

    await expect(getKnowledgeCommandStatus(workspaceId, commandId)).resolves.toEqual({
      command_id: commandId,
      status: "pending",
    });
  });
});
