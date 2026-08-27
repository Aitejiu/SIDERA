import { describe, expect, it } from "vitest";

import { resolveE2eRuntime, resolveE2eWebUrl } from "./e2eConfig";

describe("E2E runtime endpoints", () => {
  it("uses explicit control-plane and Web ports when provided", () => {
    expect(
      resolveE2eRuntime({
        SIDERA_E2E_CONTROL_PLANE_PORT: "8001",
        SIDERA_E2E_WEB_PORT: "4174",
        SIDERA_E2E_KNOWLEDGE_SERVICE_PORT: "8021",
        SIDERA_E2E_MAIN_AGENT_PORT: "8011",
      }),
    ).toEqual({
      controlPlanePort: 8001,
      knowledgeServicePort: 8021,
      mainAgentPort: 8011,
      webPort: 4174,
    });
  });

  it("falls back to the local deterministic E2E ports", () => {
    expect(resolveE2eRuntime({})).toEqual({
      controlPlanePort: 8000,
      knowledgeServicePort: 8020,
      mainAgentPort: 8010,
      webPort: 4173,
    });
  });

  it("supports dedicated Web proxy ports for the local stack", () => {
    expect(
      resolveE2eRuntime({
        SIDERA_WEB_CONTROL_PLANE_PORT: "8100",
        SIDERA_WEB_KNOWLEDGE_SERVICE_PORT: "8120",
        SIDERA_WEB_MAIN_AGENT_PORT: "8110",
        SIDERA_WEB_PORT: "4173",
      }),
    ).toEqual({
      controlPlanePort: 8100,
      knowledgeServicePort: 8120,
      mainAgentPort: 8110,
      webPort: 4173,
    });
  });

  it("gives explicit E2E ports precedence over local Web proxy ports", () => {
    expect(
      resolveE2eRuntime({
        SIDERA_E2E_CONTROL_PLANE_PORT: "18000",
        SIDERA_E2E_KNOWLEDGE_SERVICE_PORT: "18020",
        SIDERA_E2E_MAIN_AGENT_PORT: "18010",
        SIDERA_E2E_WEB_PORT: "18417",
        SIDERA_WEB_CONTROL_PLANE_PORT: "8100",
        SIDERA_WEB_KNOWLEDGE_SERVICE_PORT: "8120",
        SIDERA_WEB_MAIN_AGENT_PORT: "8110",
        SIDERA_WEB_PORT: "4173",
      }),
    ).toEqual({
      controlPlanePort: 18000,
      knowledgeServicePort: 18020,
      mainAgentPort: 18010,
      webPort: 18417,
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
