import { describe, expect, it } from "vitest";

import { resolveE2eRuntime, resolveE2eWebUrl } from "./e2eConfig";

describe("E2E runtime endpoints", () => {
  it("uses explicit control-plane and Web ports when provided", () => {
    expect(
      resolveE2eRuntime({
        SIDERA_E2E_CONTROL_PLANE_PORT: "8001",
        SIDERA_E2E_WEB_PORT: "4174",
      }),
    ).toEqual({ controlPlanePort: 8001, webPort: 4174 });
  });

  it("falls back to the local deterministic E2E ports", () => {
    expect(resolveE2eRuntime({})).toEqual({ controlPlanePort: 8000, webPort: 4173 });
  });

  it("builds the Web readiness URL from the selected Web port", () => {
    expect(resolveE2eWebUrl(4174)).toBe("http://127.0.0.1:4174/");
  });
});
