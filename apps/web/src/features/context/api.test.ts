import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { contextPackageQueryKey, getContextPackage } from "./api";

const workspaceId = "20000000-0000-0000-0000-000000000001";
const packageId = "70000000-0000-0000-0000-000000000001";

describe("ContextPackage API client", () => {
  it("uses the control-plane API and its own query key namespace", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${workspaceId}/context-packages/${packageId}`,
        () => HttpResponse.json({ package_id: packageId, status: "active" }),
      ),
    );

    await expect(getContextPackage(workspaceId, packageId)).resolves.toEqual({
      package_id: packageId,
      status: "active",
    });
    expect(contextPackageQueryKey(workspaceId, packageId)).toEqual([
      "context-package",
      workspaceId,
      packageId,
    ]);
  });
});
