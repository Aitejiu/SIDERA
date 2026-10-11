import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";
import type { CommandRequest, CommandResponse, CommandStatusResponse } from "./types";
import { buildNativeCommand, lookupNativeCommand, submitNativeCommand } from "./commands";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const REQUEST_ID = "30000000-0000-0000-0000-000000000001";
const COMMAND_ID = "40000000-0000-0000-0000-000000000001";
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: "principal-current",
  expires_at: "2099-01-01T00:00:00Z",
};

function originalCommand(overrides: Partial<CommandRequest> = {}): CommandRequest {
  return {
    command_id: COMMAND_ID,
    workspace_id: WORKSPACE_ID,
    command_type: "ConfirmPlanningProposal",
    actor: { type: "user", id: SESSION.principal_id },
    target_type: "planning_request",
    target_id: REQUEST_ID,
    expected_version: 8,
    idempotency_key: "60000000-0000-0000-0000-000000000001",
    correlation_id: "70000000-0000-0000-0000-000000000001",
    payload: { expected_proposal_revision: 2 },
    ...overrides,
  };
}

const result: CommandResponse = {
  command_id: COMMAND_ID,
  aggregate_id: REQUEST_ID,
  aggregate_version: 9,
  event_ids: ["80000000-0000-0000-0000-000000000001"],
};

afterEach(() => {
  server.resetHandlers();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
  document.cookie = "sidera_local_session=; Max-Age=0; Path=/";
  vi.restoreAllMocks();
});

describe("native original command helpers", () => {
  it("builds a frozen current-user command once and detaches it from the source draft", () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("40000000-0000-0000-0000-000000000001")
      .mockReturnValueOnce("60000000-0000-0000-0000-000000000001")
      .mockReturnValueOnce("70000000-0000-0000-0000-000000000001");
    const payload = { decisions: [{ target_key: "review", nested: { owner: "profile-r1" } }] };

    const command = buildNativeCommand({
      session: SESSION,
      workspaceId: WORKSPACE_ID,
      commandType: "ConfirmPlanningProposal",
      targetType: "planning_request",
      targetId: REQUEST_ID,
      expectedVersion: 8,
      payload,
    });
    payload.decisions[0]!.nested.owner = "changed-after-build";
    const builtPayload = command.payload as {
      decisions: Array<{ target_key: string; nested: { owner: string } }>;
    };

    expect(command).toMatchObject({
      command_id: "40000000-0000-0000-0000-000000000001",
      workspace_id: WORKSPACE_ID,
      command_type: "ConfirmPlanningProposal",
      actor: { type: "user", id: SESSION.principal_id },
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 8,
      idempotency_key: "60000000-0000-0000-0000-000000000001",
      correlation_id: "70000000-0000-0000-0000-000000000001",
      payload: { decisions: [{ target_key: "review", nested: { owner: "profile-r1" } }] },
    });
    expect(randomUUID).toHaveBeenCalledTimes(3);
    expect(Object.isFrozen(command)).toBe(true);
    expect(Object.isFrozen(command.actor)).toBe(true);
    expect(Object.isFrozen(command.payload)).toBe(true);
    expect(Object.isFrozen(builtPayload.decisions)).toBe(true);
    expect(Object.isFrozen(builtPayload.decisions[0])).toBe(true);
    expect(Object.isFrozen(builtPayload.decisions[0]!.nested)).toBe(true);
  });

  it.each([
    ["foreign workspace", { ...SESSION, workspace_id: "foreign-workspace" }, WORKSPACE_ID],
    ["empty principal", { ...SESSION, principal_id: " " }, WORKSPACE_ID],
    ["expired session", { ...SESSION, expires_at: "2000-01-01T00:00:00Z" }, WORKSPACE_ID],
    ["invalid expiry", { ...SESSION, expires_at: "not-a-date" }, WORKSPACE_ID],
  ] as const)("refuses a %s before building a command", (_name, session, workspaceId) => {
    expect(() => buildNativeCommand({
      session,
      workspaceId,
      commandType: "ConfirmPlanningProposal",
      targetType: "planning_request",
      targetId: REQUEST_ID,
      expectedVersion: 8,
      payload: {},
    })).toThrow("Assignment data is unavailable.");
  });

  it("posts the exact original JSON with the existing same-origin CSRF transport", async () => {
    document.cookie = "sidera_csrf=owned%2Dcsrf; Path=/";
    document.cookie = "sidera_local_session=never-forward-this; Path=/";
    const original = originalCommand();
    let posts = 0;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      expect(request.credentials).toBe("same-origin");
      expect(request.headers.get("X-Sidera-CSRF")).toBe("owned-csrf");
      expect(request.headers.get("Authorization")).toBeNull();
      const body = await request.json();
      expect(body).toEqual(original);
      expect(JSON.stringify(body)).not.toContain("never-forward-this");
      return HttpResponse.json(result);
    }));

    await expect(submitNativeCommand(original)).resolves.toEqual(result);
    expect(posts).toBe(1);
  });

  it("does not expose raw server details from a failed submission", async () => {
    server.use(http.post("/api/v1/commands", () => HttpResponse.json({
      detail: { code: "store_unavailable", message: "PRIVATE_DRIVER_DETAIL" },
    }, { status: 503 })));

    await expect(submitNativeCommand(originalCommand()))
      .rejects.toMatchObject({ code: "assignment_unavailable" });
    await expect(submitNativeCommand(originalCommand()))
      .rejects.not.toThrow("PRIVATE_DRIVER_DETAIL");
  });

  it("looks up the original command after a lost POST reply without posting or creating identities", async () => {
    const original = originalCommand({ command_id: "command/one", workspace_id: "workspace/one" });
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    let posts = 0;
    let reads = 0;
    let seenPath = "";
    server.use(
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.error();
      }),
      http.get(/\/api\/v1\/workspaces\/.*\/commands\/.*/, ({ request }) => {
        reads += 1;
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json({
          command_id: original.command_id,
          command_type: original.command_type,
          target_type: original.target_type,
          target_id: original.target_id,
          expected_version: original.expected_version,
          status: "applied",
          result: null,
        } satisfies CommandStatusResponse);
      }),
    );

    await expect(submitNativeCommand(original)).rejects.toBeDefined();
    const observed = await lookupNativeCommand(original);

    expect(observed.status).toBe("applied");
    expect(observed.result).toBeNull();
    expect(seenPath).toBe("/api/v1/workspaces/workspace%2Fone/commands/command%2Fone");
    expect(posts).toBe(1);
    expect(reads).toBe(1);
    expect(randomUUID).not.toHaveBeenCalled();
  });

  it.each([
    ["command", { command_id: "another-command" }],
    ["type", { command_type: "RejectPlanningProposal" }],
    ["target type", { target_type: "task" }],
    ["target id", { target_id: "another-target" }],
    ["version", { expected_version: 9 }],
    ["result command", { result: { ...result, command_id: "another-result" } }],
  ] as const)("does not accept a status with mismatched %s identity", async (_name, changed) => {
    const original = originalCommand();
    server.use(http.get(`${BASE}/commands/${COMMAND_ID}`, () => HttpResponse.json({
      command_id: original.command_id,
      command_type: original.command_type,
      target_type: original.target_type,
      target_id: original.target_id,
      expected_version: original.expected_version,
      status: "applied",
      result,
      ...changed,
    })));

    await expect(lookupNativeCommand(original)).rejects.toMatchObject({
      code: "assignment_unavailable",
    });
  });
});
