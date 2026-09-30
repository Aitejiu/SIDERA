import { type Page } from "@playwright/test";

import {
  classifyPlanningProposalGate,
  type PlanningProposalGateSnapshot,
} from "../../../src/e2e/basicCapabilityRealBoundary";

export async function waitForProposalOrPlanningFailure(
  page: Page,
  timeoutMs: number,
): Promise<void> {
  const snapshotHandle = await page.waitForFunction(
    () => {
      const suffix = (selector: string, prefix: string): string | null => {
        const element = document.querySelector(selector);
        if (element === null) return null;
        const matchingClass = Array.from(element.classList).find((value) => (
          value.startsWith(prefix)
        ));
        return matchingClass?.slice(prefix.length) ?? null;
      };
      const visible = (selector: string): boolean => {
        const element = document.querySelector(selector);
        return element !== null
          && element.getClientRects().length > 0
          && getComputedStyle(element).visibility !== "hidden"
          && getComputedStyle(element).display !== "none";
      };
      const snapshot = {
        proposalVisible: visible("#planning-proposal-title") || visible("#plan-revision-title"),
        requestStatus: suffix(".planning-status", "planning-status--")
          ?? suffix(".plan-revision-status", "plan-revision-status--"),
        runStatus: suffix(".planning-run-status", "planning-run-status--"),
      };
      const terminal = [snapshot.requestStatus, snapshot.runStatus].some((status) => (
        status === "failed" || status === "blocked"
      ));
      return terminal || (snapshot.requestStatus === "proposal_ready" && snapshot.proposalVisible)
        ? snapshot
        : false;
    },
    null,
    { timeout: timeoutMs, polling: 100 },
  );
  const snapshot = await snapshotHandle.jsonValue() as PlanningProposalGateSnapshot;
  const gate = classifyPlanningProposalGate(snapshot);
  if (gate === "failed") throw new Error("planning_failed_before_proposal");
  if (gate === "blocked") throw new Error("planning_blocked_before_proposal");
  if (gate !== "ready") throw new Error("planning_proposal_gate_unexpected_state");
}
