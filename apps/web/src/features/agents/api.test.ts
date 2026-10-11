import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";
import type { components } from "@/shared/api/product-schema";
import * as api from "./api";

const WS = "20000000-0000-0000-0000-000000000001";
const ID = "30000000-0000-0000-0000-000000000001";
const REF = { workspace_id: WS, profile_id: ID, revision: 1 };
const BASE = `/agents/v1/workspaces/${WS}`;
const draft: components["schemas"]["ProfileDraft-Input"] = {
  name: "Reusable reviewer", description: "", instructions: "Reviewed instructions",
  runtime_ref: { workspace_id: WS, runtime_id: ID }, resource_refs: [], model_id: null,
  thinking: null, max_active: 1, environment: {}, secret_refs: [], native_settings: {},
};

afterEach(() => { document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

describe("typed native agents client", () => {
  it("reads Product task tool metadata with the current local session and no write", async () => {
    let reads = 0;
    const metadata = [{ workspace_id: WS, connection_id: ID, name: "SIDERA Task Tools", availability: "configured" }];
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    server.use(http.get(`/product/v1/workspaces/${WS}/tool-connections`, ({ request }) => {
      reads += 1;
      expect(request.credentials).toBe("same-origin");
      expect(request.cache).toBe("no-store");
      expect(request.headers.get("Accept")).toBe("application/json");
      expect(request.headers.get("X-Sidera-CSRF")).toBeNull();
      return HttpResponse.json(metadata);
    }));
    expect(await api.listProductToolConnections(WS)).toEqual(metadata);
    expect(reads).toBe(1);
  });

  it("preserves exact operation/draft, same-origin CSRF and pending202 without another write", async () => {
    let writes = 0;
    const operationId = " original/save 1 ";
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    server.use(http.post(BASE + "/profiles", async ({ request }) => {
      writes += 1;
      expect(request.credentials).toBe("same-origin");
      expect(request.headers.get("X-Sidera-CSRF")).toBe("owned-csrf");
      expect(await request.json()).toEqual({ operationId, draft });
      return HttpResponse.json({ operationId, state: "unknown", target_ref: REF, result_ref: null, reason_code: "configuration_write_unknown" }, { status: 202 });
    }));
    expect(await api.saveProfile(WS, operationId, draft)).toEqual({ operationId, state: "unknown", target_ref: REF, result_ref: null, reason_code: "configuration_write_unknown" });
    expect(writes).toBe(1);
  });

  it("recovers an opaque original query string without a POST or operation-query path", async () => {
    const operationId = "opaque/slash + original";
    let reads = 0, writes = 0;
    server.use(
      http.get(`/product/v1/workspaces/${WS}/configuration-requests`, ({ request }) => {
        reads += 1;
        expect(new URL(request.url).searchParams.get("operationId")).toBe(operationId);
        expect(request.headers.get("X-Sidera-CSRF")).toBeNull();
        return HttpResponse.json({ operationId, state: "reconciled", target_ref: REF, result_ref: REF, reason_code: null });
      }),
      http.post(BASE + "/profiles", () => { writes += 1; return HttpResponse.error(); }),
    );
    expect((await api.readConfigurationRequest(WS, operationId)).result_ref).toEqual(REF);
    expect(reads).toBe(1);
    expect(writes).toBe(0);
  });

  it("keeps a definitive CAS rejection as the original finite result", async () => {
    const result = { operationId: "revision", state: "rejected", target_ref: { ...REF, revision: 2 }, result_ref: null, reason_code: "revision_conflict" };
    server.use(http.post(BASE + `/profiles/${ID}/revisions`, async ({ request }) => {
      expect(await request.json()).toEqual({ operationId: "revision", expected_ref: REF, draft });
      return HttpResponse.json(result, { status: 409 });
    }));
    expect(await api.saveProfile(WS, "revision", draft, REF)).toEqual(result);
  });

  it("uses the mounted Work cursor and retains actual conversation/task reference fields", async () => {
    const page = { task_work: { items: [], next_cursor: "next-original" }, conversations: [{ conversation_id: ID, title: "Original chat", profile_ref: REF, run_id: null, stop_state: null, updated_at: "2026-10-05T12:00:00Z" }] };
    server.use(http.get(BASE + `/profiles/${ID}/work`, ({ request }) => {
      const url = new URL(request.url);
      expect(url.searchParams.get("cursor")).toBe("original/cursor");
      expect(url.searchParams.get("limit")).toBe("10");
      return HttpResponse.json(page);
    }));
    expect(await api.listProfileWork(WS, ID, "original/cursor", 10)).toEqual(page);
  });

  it.each(["denied", "unavailable"])("does not replace %s metadata with empty success or echo server details", async kind => {
    server.use(http.get(BASE + "/profiles", () => HttpResponse.json({ detail: { code: kind === "denied" ? "permission_denied" : "store_unavailable", message: "PRIVATE_DRIVER_DETAIL" } }, { status: kind === "denied" ? 403 : 503 })));
    await expect(api.listProfiles(WS)).rejects.toMatchObject({ code: kind === "denied" ? "permission_denied" : "store_unavailable" });
    await expect(api.listProfiles(WS)).rejects.not.toThrow("PRIVATE_DRIVER_DETAIL");
  });

  it("loads only metadata and validates only the explicitly saved revision", async () => {
    const reads: string[] = [], writes: string[] = [];
    server.use(http.all(BASE + "/*", ({ request }) => {
      const url = new URL(request.url);
      if (request.method !== "GET") writes.push(url.pathname);
      reads.push(url.pathname + url.search);
      if (url.pathname.endsWith("/validation")) return HttpResponse.json({ valid: false, issues: [{ field: "mcp", code: "capability_unconfirmed", message: "Not confirmed" }], effective_preview: null });
      return HttpResponse.json(url.pathname.endsWith("runtime-descriptors") ? [] : { items: [] });
    }));
    await api.listProfiles(WS);
    await api.listResources(WS);
    await api.listRuntimes(WS);
    await api.listDescriptors(WS);
    expect((await api.validateProfile({ ...REF, revision: 3 })).valid).toBe(false);
    expect(reads.at(-1)).toBe(BASE + `/profiles/${ID}/validation?revision=3`);
    expect(reads).toHaveLength(5);
    expect(writes).toEqual([]);
  });
});
