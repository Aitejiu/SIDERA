import { test, expect, nativeRecords, assertNativeNavigation } from "./native-harness-fixtures.js";

test("native original-only desktop resume", async ({ native }) => {
  const accepted = nativeRecords(native.manifest).filter(record => record.step === "result_accepted" && record.status === "passed").at(-1);
  if (!accepted) throw new Error("native_original_accepted_checkpoint_required");
  const originals = structuredClone(accepted.original_ids);
  await native.authorize();
  expect(await native.observe(accepted)).toBe(true);
  native.stage = "navigation_history";
  native.stepId = crypto.randomUUID();
  native.record("pending", { resume: true });
  try {
    const observed = await assertNativeNavigation(native);
    expect(native.ids.root_task_id).toBe(originals.root_task_id);
    expect(native.ids.planning_request_id).toBe(originals.planning_request_id);
    expect(native.ids.source_run_id).toBe(originals.source_run_id);
    expect(native.ids.source_execution_id).toBe(originals.source_execution_id);
    expect(native.ids.acceptance_command_id).toBe(originals.acceptance_command_id);
    expect(native.mutations).toEqual([]);
    native.record("passed", { ...observed, canonical: true, original_only_resume: true });
  } catch {
    native.record("failed", { reason: "native_original_navigation_assertion_failed" });
    throw new Error("native_original_navigation_assertion_failed");
  }
});
