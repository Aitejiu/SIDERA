import { describe, expect, it } from "vitest";

import { resolveBackendProxyTargets } from "./nativeBackendConfig";

describe("explicit native product proxy", () => {
  it.each([undefined, "legacy"])("preserves the legacy E2E routes for %s", (mode) => {
    expect(resolveBackendProxyTargets({ SIDERA_WEB_BACKEND: mode, SIDERA_E2E_CONTROL_PLANE_PORT: "18300", SIDERA_E2E_MAIN_AGENT_PORT: "18310" })).toEqual({
      "/api": { target: "http://127.0.0.1:18300", changeOrigin: false },
      "/healthz": { target: "http://127.0.0.1:18300", changeOrigin: false },
      "/main-agent": { target: "http://127.0.0.1:18310", changeOrigin: false },
    });
  });

  it("routes all six native prefixes to one owner while preserving Host and Origin", () => {
    const targets = resolveBackendProxyTargets({ SIDERA_WEB_BACKEND: "native", SIDERA_WEB_PRODUCT_URL: "http://127.0.0.1:18080/" });
    expect(Object.keys(targets)).toEqual(["/api", "/healthz", "/main-agent", "/product", "/agents", "/model-config"]);
    for (const target of Object.values(targets)) {
      expect(target).toEqual({ target: "http://127.0.0.1:18080", changeOrigin: false });
    }
    expect(targets).not.toHaveProperty("/knowledge-api");
  });

  it.each([undefined, "", "http://127.0.0.1", "https://127.0.0.1:18080", "http://localhost:18080", "http://192.0.2.1:18080", "http://secret@127.0.0.1:18080", "http://127.0.0.1:18080/path", "http://127.0.0.1:18080/?key=secret", "http://127.0.0.1:18080/#secret", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:bad", " http://127.0.0.1:18080"])("rejects invalid native selection without a legacy fallback: %s", (url) => {
    expect(() => resolveBackendProxyTargets({ SIDERA_WEB_BACKEND: "native", SIDERA_WEB_PRODUCT_URL: url })).toThrow("SIDERA_WEB_PRODUCT_URL");
  });

  it("rejects an unknown backend mode rather than using legacy routes", () => {
    expect(() => resolveBackendProxyTargets({ SIDERA_WEB_BACKEND: "other" })).toThrow("SIDERA_WEB_BACKEND");
  });
});
