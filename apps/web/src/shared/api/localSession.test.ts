import { afterEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";

import { server } from "@/test/server";
import { apiCommand, apiRequestJson } from "./client";
import { localSessionRequest } from "./localSession";

afterEach(() => {
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
  document.cookie = "sidera_local_session=; Max-Age=0; Path=/";
});

describe("local browser writer boundary", () => {
  it.each(["POST", "PUT", "PATCH", "DELETE"])("adds only the exact readable CSRF cookie on %s", (method) => {
    document.cookie = "sidera_csrf=owned%2Dcsrf; Path=/";
    document.cookie = "sidera_local_session=NEVER_FORWARD_SESSION; Path=/";
    const init = localSessionRequest({ method, headers: new Headers({ "X-Correlation-Id": "original", "x-sidera-csrf": "wrong" }) });
    const headers = new Headers(init.headers);
    expect(headers.get("X-Sidera-CSRF")).toBe("owned-csrf");
    expect(headers.get("X-Correlation-Id")).toBe("original");
    expect(headers.get("Authorization")).toBeNull();
    expect([...headers.values()].join(" ")).not.toContain("NEVER_FORWARD_SESSION");
    expect(init.credentials).toBe("same-origin");
  });

  it.each(["GET", "HEAD", "OPTIONS"])("does not add a CSRF header to %s", (method) => {
    document.cookie = "sidera_csrf=readable; Path=/";
    const init = localSessionRequest({ method, headers: [["X-Sidera-CSRF", "stale"], ["Accept", "application/json"]] });
    expect(new Headers(init.headers).get("X-Sidera-CSRF")).toBeNull();
    expect(new Headers(init.headers).get("Accept")).toBe("application/json");
  });

  it.each(["", "%ZZ"])("does not substitute a session token for a missing or malformed nonce %s", (value) => {
    document.cookie = `sidera_csrf=${value}; Path=/`;
    document.cookie = "sidera_local_session=NEVER_FORWARD_SESSION; Path=/";
    const init = localSessionRequest({ method: "POST" });
    expect(new Headers(init.headers).get("X-Sidera-CSRF")).toBeNull();
  });

  it("applies the same writer boundary to shared JSON and command requests without losing Headers", async () => {
    document.cookie = "sidera_csrf=shared-owned; Path=/";
    const observed: Array<{ csrf: string | null; correlation: string | null; credentials: string }> = [];
    server.use(http.post("/api/v1/commands", ({ request }) => {
      observed.push({ csrf: request.headers.get("X-Sidera-CSRF"), correlation: request.headers.get("X-Correlation-Id"), credentials: request.credentials });
      return HttpResponse.json({ retained: true });
    }));
    const init = { method: "POST", body: "{}", headers: new Headers({ "X-Correlation-Id": "same-command" }) };
    await expect(apiRequestJson("/api/v1/commands", init)).resolves.toEqual({ retained: true });
    await expect(apiCommand("/api/v1/commands", init)).resolves.toEqual({ retained: true });
    expect(observed).toEqual([
      { csrf: "shared-owned", correlation: "same-command", credentials: "same-origin" },
      { csrf: "shared-owned", correlation: "same-command", credentials: "same-origin" },
    ]);
  });
});
