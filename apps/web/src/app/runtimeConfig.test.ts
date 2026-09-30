import { describe, expect, it } from "vitest";

import { FALLBACK_WORKSPACE_ID, resolveDefaultWorkspaceId } from "./runtimeConfig";

describe("runtime configuration", () => {
  it("uses the workspace injected by the local runtime", () => {
    expect(resolveDefaultWorkspaceId(" 1d8fdbee-8a92-4284-b010-48038f2690c3 ")).toBe(
      "1d8fdbee-8a92-4284-b010-48038f2690c3",
    );
  });

  it("keeps the deterministic development fallback when no workspace is injected", () => {
    expect(resolveDefaultWorkspaceId(undefined)).toBe(FALLBACK_WORKSPACE_ID);
    expect(resolveDefaultWorkspaceId("   ")).toBe(FALLBACK_WORKSPACE_ID);
  });
});
