import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import {
  readBusinessCommandAuthority,
  reviewBusinessCommandAuthority,
  type BusinessAuthorityReview,
} from "./businessAuthority";

const WORKSPACE_ID = "workspace-a";
const NOW = Date.parse("2026-10-07T00:00:00.000Z");
const PRODUCT_HEALTH = {
  status: "ok",
  service: "sidera-product",
  provider: "available",
};
const LEGACY_HEALTH = {
  status: "ok",
  service: "sidera-main-agent-gateway",
  provider: "available",
};
const SESSION = {
  session_id: "session-a",
  workspace_id: WORKSPACE_ID,
  principal_id: "principal-a",
  expires_at: "2026-10-07T01:00:00.000Z",
};

function review(overrides: Partial<BusinessAuthorityReview> = {}) {
  return reviewBusinessCommandAuthority({
    workspaceId: WORKSPACE_ID,
    health: PRODUCT_HEALTH,
    session: SESSION,
    now: NOW,
    ...overrides,
  });
}

afterEach(() => vi.restoreAllMocks());

describe("reviewBusinessCommandAuthority", () => {
  const invalidHealth: { label: string; health: unknown }[] = [
    { label: "missing", health: undefined },
    { label: "null", health: null },
    { label: "string", health: "sidera-product" },
    { label: "number", health: 1 },
    { label: "boolean", health: true },
    { label: "array", health: [PRODUCT_HEALTH] },
    { label: "empty record", health: {} },
    { label: "missing status", health: { service: "sidera-product" } },
    { label: "non-ok status", health: { ...PRODUCT_HEALTH, status: "degraded" } },
    { label: "boolean status", health: { ...PRODUCT_HEALTH, status: true } },
    { label: "missing service", health: { status: "ok" } },
    { label: "unknown service", health: { status: "ok", service: "other-host" } },
    { label: "non-string service", health: { status: "ok", service: 1 } },
    { label: "legacy without ok status", health: { ...LEGACY_HEALTH, status: "unavailable" } },
  ];

  it.each(invalidHealth)("rejects $label health without legacy fallback", ({ health }) => {
    expect(review({ health })).toEqual({ kind: "unavailable", reason: "host_unavailable" });
  });

  it.each(["", " ", "\t\n"])("rejects blank workspace %j before Product selection", (workspaceId) => {
    expect(review({ workspaceId })).toEqual({ kind: "unavailable", reason: "host_unavailable" });
  });

  it.each(["", " ", "\t\n"])("rejects blank workspace %j before legacy selection", (workspaceId) => {
    expect(review({ workspaceId, health: LEGACY_HEALTH })).toEqual({ kind: "unavailable", reason: "host_unavailable" });
  });

  it.each([NaN, Infinity, -Infinity])("rejects nonfinite now %s before Product selection", (now) => {
    expect(review({ now })).toEqual({ kind: "unavailable", reason: "host_unavailable" });
  });

  it.each([NaN, Infinity, -Infinity])("rejects nonfinite now %s before legacy selection", (now) => {
    expect(review({ now, health: LEGACY_HEALTH })).toEqual({ kind: "unavailable", reason: "host_unavailable" });
  });

  it.each([
    { label: "available provider", health: PRODUCT_HEALTH },
    { label: "unavailable provider", health: { ...PRODUCT_HEALTH, provider: "unavailable" } },
    { label: "missing provider", health: { status: "ok", service: "sidera-product" } },
  ])("accepts positive Product health with $label", ({ health }) => {
    expect(review({ health })).toEqual({ kind: "product", session: SESSION });
  });

  it.each([
    { label: "available provider", health: LEGACY_HEALTH },
    { label: "unavailable provider", health: { ...LEGACY_HEALTH, provider: "unavailable" } },
    { label: "missing provider", health: { status: "ok", service: "sidera-main-agent-gateway" } },
  ])("accepts positive legacy health with $label without inventing a human", ({ health }) => {
    expect(review({ health, session: null, expectedPrincipalId: "captured-human" })).toEqual({ kind: "legacy" });
  });

  const malformedSessions: { label: string; session: unknown }[] = [
    { label: "missing", session: undefined },
    { label: "null", session: null },
    { label: "string", session: "session-a" },
    { label: "number", session: 1 },
    { label: "boolean", session: true },
    { label: "array", session: [SESSION] },
    { label: "empty record", session: {} },
    { label: "missing session ID", session: { workspace_id: WORKSPACE_ID, principal_id: "principal-a", expires_at: SESSION.expires_at } },
    { label: "missing workspace ID", session: { session_id: "session-a", principal_id: "principal-a", expires_at: SESSION.expires_at } },
    { label: "missing principal ID", session: { session_id: "session-a", workspace_id: WORKSPACE_ID, expires_at: SESSION.expires_at } },
    { label: "missing expiry", session: { session_id: "session-a", workspace_id: WORKSPACE_ID, principal_id: "principal-a" } },
    { label: "empty session ID", session: { ...SESSION, session_id: "" } },
    { label: "blank session ID", session: { ...SESSION, session_id: " \t" } },
    { label: "non-string session ID", session: { ...SESSION, session_id: 1 } },
    { label: "empty workspace ID", session: { ...SESSION, workspace_id: "" } },
    { label: "blank workspace ID", session: { ...SESSION, workspace_id: " \t" } },
    { label: "non-string workspace ID", session: { ...SESSION, workspace_id: 1 } },
    { label: "empty principal ID", session: { ...SESSION, principal_id: "" } },
    { label: "blank principal ID", session: { ...SESSION, principal_id: " \t" } },
    { label: "non-string principal ID", session: { ...SESSION, principal_id: 1 } },
    { label: "empty expiry", session: { ...SESSION, expires_at: "" } },
    { label: "blank expiry", session: { ...SESSION, expires_at: " \t" } },
    { label: "non-string expiry", session: { ...SESSION, expires_at: NOW + 1 } },
    { label: "invalid expiry", session: { ...SESSION, expires_at: "not-a-date" } },
    { label: "out-of-range expiry", session: { ...SESSION, expires_at: "+999999-01-01T00:00:00.000Z" } },
  ];

  it.each(malformedSessions)("rejects $label Product session without legacy fallback", ({ session }) => {
    expect(review({ session })).toEqual({ kind: "unavailable", reason: "session_unavailable" });
  });

  it("rejects a valid session belonging to another workspace", () => {
    expect(review({ session: { ...SESSION, workspace_id: "workspace-b" } })).toEqual({ kind: "unavailable", reason: "session_mismatch" });
  });

  it("rejects a current principal different from the captured owner", () => {
    expect(review({ expectedPrincipalId: "principal-b" })).toEqual({ kind: "unavailable", reason: "session_mismatch" });
  });

  it("accepts the exact captured current principal", () => {
    expect(review({ expectedPrincipalId: "principal-a" })).toEqual({ kind: "product", session: SESSION });
  });

  it("returns the newer current principal when no owner was captured", () => {
    expect(review({ session: { ...SESSION, principal_id: "principal-b" } })).toEqual({
      kind: "product",
      session: { ...SESSION, principal_id: "principal-b" },
    });
  });

  it.each([
    { label: "before now", expires_at: "2026-10-06T23:59:59.999Z" },
    { label: "equal to now", expires_at: "2026-10-07T00:00:00.000Z" },
  ])("rejects an expiry $label", ({ expires_at }) => {
    expect(review({ session: { ...SESSION, expires_at } })).toEqual({ kind: "unavailable", reason: "session_expired" });
  });

  it("accepts an expiry strictly greater than now", () => {
    expect(review({ session: { ...SESSION, expires_at: "2026-10-07T00:00:00.001Z" } })).toEqual({
      kind: "product",
      session: { ...SESSION, expires_at: "2026-10-07T00:00:00.001Z" },
    });
  });

  it("projects only the four public session fields", () => {
    const wireSession = { ...SESSION, token: "private-fixture", csrf: "private-fixture", nested: { extra: true } };
    const authority = review({ session: wireSession });

    expect(authority).toEqual({ kind: "product", session: SESSION });
    if (authority.kind !== "product") throw new Error("Expected Product authority");
    expect(authority.session).not.toBe(wireSession);
  });

  it("preserves nonblank public identity values without trimming", () => {
    const session = { ...SESSION, session_id: " session-a ", workspace_id: " workspace-a ", principal_id: " principal-a " };
    expect(review({ workspaceId: " workspace-a ", expectedPrincipalId: " principal-a ", session })).toEqual({ kind: "product", session });
  });

  it("requires exact workspace identity instead of matching a trimmed value", () => {
    expect(review({ session: { ...SESSION, workspace_id: " workspace-a " } })).toEqual({ kind: "unavailable", reason: "session_mismatch" });
  });

  it("requires exact captured principal identity instead of matching a trimmed value", () => {
    expect(review({ expectedPrincipalId: " principal-a " })).toEqual({ kind: "unavailable", reason: "session_mismatch" });
  });

  it("uses the supplied clock without sampling time or allocating identity", () => {
    vi.spyOn(Date, "now").mockImplementation(() => { throw new Error("Pure review must use its supplied clock"); });
    const identity = vi.spyOn(crypto, "randomUUID");

    expect(review()).toEqual({ kind: "product", session: SESSION });
    expect(identity).not.toHaveBeenCalled();
  });
});

describe("readBusinessCommandAuthority with existing transports", () => {
  const HEALTH_PATH = "/main-agent/v1/health";
  const SESSION_PATH = "/product/v1/session";
  const observedRequests = new Map<string, number>();
  let requests: Request[] = [];
  let clockNow = NOW;
  let identityAllocations = 0;
  let storageWrites = 0;
  let totalIdentityAllocations = 0;
  let totalStorageWrites = 0;

  function observeRequest({ request }: { request: Request }) {
    requests.push(request);
    const key = `${request.method} ${new URL(request.url).pathname}`;
    observedRequests.set(key, (observedRequests.get(key) ?? 0) + 1);
  }

  function expectRequests(expected: string[]) {
    expect(requests.map(request => `${request.method} ${new URL(request.url).pathname}`)).toEqual(expected);
  }

  function productReads() {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json(SESSION)),
    );
  }

  beforeAll(() => server.events.on("request:start", observeRequest));
  afterAll(() => {
    server.events.removeListener("request:start", observeRequest);
    console.info("N22 authority observed requests", JSON.stringify({
      requests: Object.fromEntries(observedRequests),
      identityAllocations: totalIdentityAllocations,
      storageWrites: totalStorageWrites,
    }));
  });

  beforeEach(() => {
    requests = [];
    clockNow = NOW;
    identityAllocations = 0;
    storageWrites = 0;
    vi.spyOn(Date, "now").mockImplementation(() => clockNow);
    vi.spyOn(crypto, "randomUUID").mockImplementation(() => {
      identityAllocations += 1;
      return "00000000-0000-4000-8000-000000000000";
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { storageWrites += 1; });
    server.use(http.all("*", () => HttpResponse.json({ detail: { code: "unexpected_request" } }, { status: 418 })));
  });

  afterEach(() => {
    totalIdentityAllocations += identityAllocations;
    totalStorageWrites += storageWrites;
    expect(identityAllocations).toBe(0);
    expect(storageWrites).toBe(0);
    for (const request of requests) {
      expect(request.method).toBe("GET");
      expect([HEALTH_PATH, SESSION_PATH]).toContain(new URL(request.url).pathname);
      expect(request.credentials).toBe("same-origin");
      expect(request.headers.get("Accept")).toBe("application/json");
      expect(request.headers.has("X-Sidera-CSRF")).toBe(false);
      expect(request.headers.has("Authorization")).toBe(false);
    }
  });

  it.each([
    { label: "available provider", health: PRODUCT_HEALTH },
    { label: "unavailable provider", health: { ...PRODUCT_HEALTH, provider: "unavailable" } },
    { label: "missing provider", health: { status: "ok", service: "sidera-product" } },
  ])("reads the actual current human after Product health with $label", async ({ health }) => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(health)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json(SESSION)),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID, { expectedPrincipalId: "principal-a" })).toEqual({ kind: "product", session: SESSION });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it.each([
    { label: "available provider", health: LEGACY_HEALTH },
    { label: "unavailable provider", health: { ...LEGACY_HEALTH, provider: "unavailable" } },
    { label: "missing provider", health: { status: "ok", service: "sidera-main-agent-gateway" } },
  ])("reads no Product session after positive legacy health with $label", async ({ health }) => {
    server.use(http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(health)));

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "legacy" });
    expectRequests(["GET /main-agent/v1/health"]);
  });

  it.each(["", " ", "\t\n"])("reads no transport for blank workspace %j", async (workspaceId) => {
    productReads();

    expect(await readBusinessCommandAuthority(workspaceId)).toEqual({ kind: "unavailable", reason: "host_unavailable" });
    expectRequests([]);
  });

  it.each([401, 403, 503])("fails health HTTP %s closed without a session read", async (status) => {
    server.use(http.get(`*${HEALTH_PATH}`, () => HttpResponse.json({
      error: { code: "host_denied", message: "private-server-detail", retryable: status === 503 },
    }, { status })));

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "host_unavailable" });
    expectRequests(["GET /main-agent/v1/health"]);
  });

  it("fails a lost health response closed without another endpoint", async () => {
    server.use(http.get(`*${HEALTH_PATH}`, () => HttpResponse.error()));

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "host_unavailable" });
    expectRequests(["GET /main-agent/v1/health"]);
  });

  it("fails invalid health JSON closed without a session read", async () => {
    server.use(http.get(`*${HEALTH_PATH}`, () => new HttpResponse("{", { headers: { "Content-Type": "application/json" } })));

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "host_unavailable" });
    expectRequests(["GET /main-agent/v1/health"]);
  });

  it.each([
    { label: "null", body: null },
    { label: "array", body: [PRODUCT_HEALTH] },
    { label: "missing fields", body: {} },
    { label: "unknown service", body: { status: "ok", service: "unknown-host" } },
    { label: "non-ok Product", body: { ...PRODUCT_HEALTH, status: "failed" } },
    { label: "non-ok legacy", body: { ...LEGACY_HEALTH, status: "failed" } },
  ])("fails $label health success closed without legacy fallback", async ({ body }) => {
    server.use(http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(body)));

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "host_unavailable" });
    expectRequests(["GET /main-agent/v1/health"]);
  });

  it.each([401, 403, 503])("fails session HTTP %s closed without bootstrap or commands", async (status) => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json({ detail: { code: "permission_denied", message: "private-server-detail" } }, { status })),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "session_unavailable" });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it("fails a lost session response closed without another read or write", async () => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.error()),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "session_unavailable" });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it("fails invalid session JSON closed without a write", async () => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => new HttpResponse("{", { headers: { "Content-Type": "application/json" } })),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "session_unavailable" });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it.each([
    { label: "null", body: null },
    { label: "array", body: [SESSION] },
    { label: "missing fields", body: {} },
    { label: "blank principal", body: { ...SESSION, principal_id: " " } },
    { label: "invalid expiry", body: { ...SESSION, expires_at: "not-a-date" } },
  ])("fails $label session success closed without legacy fallback", async ({ body }) => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json(body)),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "session_unavailable" });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it("rejects an actual session from a different workspace", async () => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json({ ...SESSION, workspace_id: "workspace-b" })),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "session_mismatch" });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it.each([
    { label: "expired", expires_at: "2026-10-06T23:59:59.999Z" },
    { label: "expiry equal to now", expires_at: "2026-10-07T00:00:00.000Z" },
  ])("rejects an actual $label session", async ({ expires_at }) => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json({ ...SESSION, expires_at })),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "session_expired" });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it("samples a fresh clock after the session read instead of accepting entry-time freshness", async () => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => {
        clockNow = Date.parse(SESSION.expires_at);
        return HttpResponse.json(SESSION);
      }),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "unavailable", reason: "session_expired" });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it("projects the actual wire response to four public session fields", async () => {
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json({ ...SESSION, token: "private-fixture", csrf: "private-fixture" })),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "product", session: SESSION });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it("rejects the second actual session when its principal replaces a captured owner", async () => {
    let sessionReads = 0;
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => {
        sessionReads += 1;
        return HttpResponse.json(sessionReads === 1 ? SESSION : { ...SESSION, session_id: "session-b", principal_id: "principal-b" });
      }),
    );

    const first = await readBusinessCommandAuthority(WORKSPACE_ID);
    expect(first).toEqual({ kind: "product", session: SESSION });
    if (first.kind !== "product") throw new Error("Expected current Product session");
    expect(await readBusinessCommandAuthority(WORKSPACE_ID, { expectedPrincipalId: first.session.principal_id })).toEqual({ kind: "unavailable", reason: "session_mismatch" });
    expectRequests([
      "GET /main-agent/v1/health", "GET /product/v1/session",
      "GET /main-agent/v1/health", "GET /product/v1/session",
    ]);
  });

  it("returns the actual newer principal when no owner was captured", async () => {
    const newerSession = { ...SESSION, session_id: "session-b", principal_id: "principal-b" };
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, () => HttpResponse.json(newerSession)),
    );

    expect(await readBusinessCommandAuthority(WORKSPACE_ID)).toEqual({ kind: "product", session: newerSession });
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });

  it("propagates cancellation at entry without any read", async () => {
    productReads();
    const controller = new AbortController();
    controller.abort();

    await expect(readBusinessCommandAuthority(WORKSPACE_ID, { signal: controller.signal })).rejects.toMatchObject({ name: "AbortError" });
    expectRequests([]);
  });

  it("preserves the supplied cancellation reason at entry", async () => {
    productReads();
    const controller = new AbortController();
    const cancellation = new DOMException("Scope cancelled", "AbortError");
    controller.abort(cancellation);

    await expect(readBusinessCommandAuthority(WORKSPACE_ID, { signal: controller.signal })).rejects.toBe(cancellation);
    expectRequests([]);
  });

  it("propagates the supplied signal during health without reading a session", async () => {
    const controller = new AbortController();
    server.use(http.get(`*${HEALTH_PATH}`, ({ request }) => {
      expect(request.signal.aborted).toBe(false);
      controller.abort();
      expect(request.signal.aborted).toBe(true);
      return HttpResponse.json(PRODUCT_HEALTH);
    }));

    await expect(readBusinessCommandAuthority(WORKSPACE_ID, { signal: controller.signal })).rejects.toMatchObject({ name: "AbortError" });
    expectRequests(["GET /main-agent/v1/health"]);
  });

  it("preserves session cancellation despite the existing AgentApiProblem wrapper", async () => {
    const controller = new AbortController();
    const cancellation = new DOMException("Scope cancelled", "AbortError");
    server.use(
      http.get(`*${HEALTH_PATH}`, () => HttpResponse.json(PRODUCT_HEALTH)),
      http.get(`*${SESSION_PATH}`, ({ request }) => {
        expect(request.signal.aborted).toBe(false);
        controller.abort(cancellation);
        expect(request.signal.aborted).toBe(true);
        return HttpResponse.json(SESSION);
      }),
    );

    await expect(readBusinessCommandAuthority(WORKSPACE_ID, { signal: controller.signal })).rejects.toBe(cancellation);
    expectRequests(["GET /main-agent/v1/health", "GET /product/v1/session"]);
  });
});
