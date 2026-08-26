import { describe, expect, it } from "vitest";

import { resolveE2eRuntime, resolveE2eWebUrl } from "./e2eConfig";

describe("E2E runtime endpoints", () => {
  it("uses explicit control-plane and Web ports when provided", () => {
    expect(
      resolveE2eRuntime({
        SIDERA_E2E_CONTROL_PLANE_PORT: "8001",
        SIDERA_E2E_WEB_PORT: "4174",
        SIDERA_E2E_KNOWLEDGE_SERVICE_PORT: "8021",
      }),
    ).toEqual({ controlPlanePort: 8001, knowledgeServicePort: 8021, webPort: 4174 });
  });

  it("falls back to the local deterministic E2E ports", () => {
    expect(resolveE2eRuntime({})).toEqual({
      controlPlanePort: 8000,
      knowledgeServicePort: 8020,
      webPort: 4173,
    });
  });

  it("builds the Web readiness URL from the selected Web port", () => {
    expect(resolveE2eWebUrl(4174)).toBe("http://127.0.0.1:4174/");
  });

  it("keeps knowledge proxy target and rewrite explicit", async () => {
    const { knowledgeProxyTarget, rewriteKnowledgeProxyPath } = await import("./e2eConfig");

    expect(knowledgeProxyTarget(8021)).toBe("http://127.0.0.1:8021");
    expect(rewriteKnowledgeProxyPath("/knowledge-api/api/v1/health")).toBe("/api/v1/health");
    expect(rewriteKnowledgeProxyPath("/api/v1/health")).toBe("/api/v1/health");
  });
});
