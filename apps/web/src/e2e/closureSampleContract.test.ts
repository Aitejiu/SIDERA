import { describe, expect, it } from "vitest";

import {
  closureSampleAcceptanceMarkerInstruction,
  findClosureSampleIntegrationInputGroups,
  assertClosureSampleIntegrationInputs,
  assertClosureSampleIntegrationSpec,
  assertClosureSampleProposal,
  closureSampleWorkerItemSourceKey,
} from "./closureSampleContract";

describe("closure sample acceptance marker instruction", () => {
  it("requires the exact run marker in a top-level summary JSON string field", () => {
    const marker = "SIDERA-R6-BASIC-A-test123";
    const instruction = closureSampleAcceptanceMarkerInstruction(marker);

    expect(instruction).toContain("out/summary.json");
    expect(instruction).toContain('top-level JSON string property "acceptance_marker"');
    expect(instruction).toContain(JSON.stringify(marker));
  });

  it("rejects a blank marker", () => {
    expect(() => closureSampleAcceptanceMarkerInstruction("  ")).toThrow();
  });
});

type ProposalFixture = {
  display: {
    subtasks: Record<string, unknown>[];
    worker_items: Record<string, unknown>[];
    integration_spec: Record<string, unknown>;
  };
  collaboration: {
    input_groups: Record<string, unknown>[];
  };
};

type IntegrationFixture = {
  resultInputs: { groups: Record<string, unknown>[] };
  selection: Record<string, unknown>;
  resultOverview: Record<string, unknown>;
  candidateDetail: Record<string, unknown>;
};

describe("materialized closure sample WorkerItem identity", () => {
  it("maps UUID stable keys by an exact source title prefix", () => {
    expect(closureSampleWorkerItemSourceKey({
      stableKey: "worker-item:2b0ad46e-8db7-4fd0-a6b4-c4c1d0a4919f",
      title: "source-a 成果产出",
    })).toBe("source-a");
  });

  it("does not match source names that only share a prefix", () => {
    expect(closureSampleWorkerItemSourceKey({
      stableKey: "worker-item:2b0ad46e-8db7-4fd0-a6b4-c4c1d0a4919f",
      title: "source-alpha 成果产出",
    })).toBeNull();
  });

  it("maps a materialized WorkerItem by its exact output responsibility", () => {
    expect(closureSampleWorkerItemSourceKey({
      stableKey: "worker-item:2b0ad46e-8db7-4fd0-a6b4-c4c1d0a4919f",
      title: "生成 source 成果文件",
      responsibility: "仅生成 out/source-a.json，内容精确为基准 A。",
    })).toBe("source-a");
  });

  it("does not match a responsibility path that only shares a prefix", () => {
    expect(closureSampleWorkerItemSourceKey({
      stableKey: "worker-item:2b0ad46e-8db7-4fd0-a6b4-c4c1d0a4919f",
      title: "生成 source 成果文件",
      responsibility: "仅生成 out/source-alpha.json。",
    })).toBeNull();
  });

  it("rejects a stable-key and title identity conflict", () => {
    expect(closureSampleWorkerItemSourceKey({
      stableKey: "worker-source-a",
      title: "source-b 成果产出",
    })).toBeNull();
  });

  it("rejects a stable-key and responsibility identity conflict", () => {
    expect(closureSampleWorkerItemSourceKey({
      stableKey: "worker-source-a",
      title: "source-a 成果产出",
      responsibility: "仅生成 out/source-b.json。",
    })).toBeNull();
  });
});

function proposal(inputGroup: Record<string, unknown> = {}): ProposalFixture {
  return {
    display: {
      subtasks: [
        { key: "implementation", acceptance_refs: ["acceptance:0000", "acceptance:0001"] },
        { key: "integration", acceptance_refs: ["acceptance:0002", "acceptance:0003", "acceptance:0004"] },
      ],
      worker_items: [
        {
          key: "source-a",
          subtask_key: "implementation",
          responsibility: "Create the exact source-a file.",
        },
        {
          key: "source-b",
          subtask_key: "implementation",
          responsibility: "Create the exact source-b file.",
        },
        {
          key: "integration",
          subtask_key: "integration",
          responsibility: "Aggregate both sources and preserve the summary marker.",
        },
      ],
      integration_spec: {
        criteria: [{ key: "integration", text: "Preserve the summary marker." }],
      },
    },
    collaboration: {
      input_groups: [
        {
          group_key: "planner-chosen-key",
          consumer_worker_item_key: "integration",
          candidates: [
            { worker_item_key: "source-a", priority: 1 },
            { worker_item_key: "source-b", priority: 2 },
          ],
          mode: "all",
          minimum_count: null,
          requirements: {
            execution_status: "succeeded",
            required_artifact_refs: ["artifact_type:report"],
            required_evidence_refs: [],
            evidence_status: "candidate",
          },
          ...inputGroup,
        },
      ],
    },
  };
}

function integrationFacts(): IntegrationFixture {
  const selectedSources = [
    { worker_item_key: "source-a", priority: 1, effective_execution_id: "execution-a" },
    { worker_item_key: "source-b", priority: 2, effective_execution_id: "execution-b" },
  ];
  const sourceResultIds = ["snapshot-a", "snapshot-b"];
  return {
    resultInputs: {
      groups: [{
        group_key: "planner-chosen-key",
        mode: "all",
        minimum_count: null,
        required_count: 2,
        candidates: [
          { worker_item_key: "source-a", priority: 1 },
          { worker_item_key: "source-b", priority: 2 },
        ],
        selected_sources: selectedSources,
        selection_id: "selection-1",
        selection_hash: "selection-hash",
      }],
    },
    selection: {
      selection_id: "selection-1",
      selection_hash: "selection-hash",
      groups: [{
        group_key: "planner-chosen-key",
        sources: selectedSources,
      }],
    },
    resultOverview: {
      current_candidate: {
        candidate_id: "candidate-1",
        source_result_ids: sourceResultIds,
        storage_status: "available",
      },
    },
    candidateDetail: {
      candidate: {
        candidate_id: "candidate-1",
        source_result_ids: sourceResultIds,
        storage_status: "available",
      },
      source_results: [
        { snapshot_id: "snapshot-a", execution_id: "execution-a" },
        { snapshot_id: "snapshot-b", execution_id: "execution-b" },
      ],
    },
  };
}

describe("closure sample proposal contract", () => {
  it("rejects a proposal that drops the exact summary marker", () => {
    const requiredMarker = "SIDERA-R6-BASIC-A-marker";
    const assertWithMarker = assertClosureSampleProposal as (
      value: unknown,
      marker: string,
    ) => void;

    expect(() => assertWithMarker(proposal(), requiredMarker))
      .toThrow(/closure_sample_proposal_invalid/);
  });

  it.each(["worker responsibility", "acceptance criterion"])(
    "rejects a marker present only outside the %s",
    (missingLocation) => {
      const requiredMarker = "SIDERA-R6-BASIC-A-marker";
      const value = proposal();
      const integrationWorker = value.display.worker_items[2]!;
      const criterion = (value.display.integration_spec.criteria as Record<string, unknown>[])[0]!;
      if (missingLocation === "worker responsibility") {
        criterion.text = `Summary marker must be ${requiredMarker}.`;
      } else {
        integrationWorker.responsibility = `Preserve ${requiredMarker}.`;
      }

      const assertWithMarker = assertClosureSampleProposal as (
        proposal: unknown,
        marker: string,
      ) => void;
      expect(() => assertWithMarker(value, requiredMarker))
        .toThrow(/closure_sample_proposal_invalid/);
    },
  );

  it("accepts the exact marker in both integration responsibility and criterion", () => {
    const requiredMarker = "SIDERA-R6-BASIC-A-marker";
    const value = proposal();
    value.display.worker_items[2]!.responsibility = `Preserve ${requiredMarker}.`;
    const criterion = (value.display.integration_spec.criteria as Record<string, unknown>[])[0]!;
    criterion.text = `Summary marker must be exactly ${requiredMarker}.`;

    const assertWithMarker = assertClosureSampleProposal as (
      proposal: unknown,
      marker: string,
    ) => void;
    expect(() => assertWithMarker(value, requiredMarker)).not.toThrow();
  });

  it.each([
    ["all", null],
    ["at_least", 2],
  ])("accepts mode %s with minimum_count %s", (mode, minimumCount) => {
    expect(() => assertClosureSampleProposal(proposal({ mode, minimum_count: minimumCount })))
      .not.toThrow();
  });

  it("accepts generated subtask IDs when the worker grouping is correct", () => {
    const value = proposal();
    value.display.subtasks = [
      { key: "implementation-subtask", acceptance_refs: ["acceptance:0000"] },
      { key: "integration-subtask", acceptance_refs: ["acceptance:0002"] },
    ];
    value.display.worker_items = [
      { key: "source-a", subtask_key: "implementation-subtask" },
      { key: "source-b", subtask_key: "implementation-subtask" },
      { key: "integration", subtask_key: "integration-subtask" },
    ];

    expect(() => assertClosureSampleProposal(value)).not.toThrow();
  });

  it("accepts a planner-chosen integration WorkerItem key when the input group references it", () => {
    const value = proposal();
    value.display.subtasks = [
      { key: "implementation-subtask", acceptance_refs: ["acceptance:0000"] },
      { key: "integration-subtask", acceptance_refs: ["acceptance:0002"] },
    ];
    value.display.worker_items = [
      { key: "source-a", subtask_key: "implementation-subtask" },
      { key: "source-b", subtask_key: "implementation-subtask" },
      { key: "integration-worker", subtask_key: "integration-subtask" },
    ];
    value.collaboration.input_groups[0] = {
      ...value.collaboration.input_groups[0],
      consumer_worker_item_key: "integration-worker",
    };

    expect(() => assertClosureSampleProposal(value)).not.toThrow();
  });

  it("accepts worker-prefixed source keys when the input group references the exact keys", () => {
    const value = proposal();
    value.display.worker_items = [
      { key: "worker-source-a", subtask_key: "implementation" },
      { key: "worker-source-b", subtask_key: "implementation" },
      { key: "worker-integration", subtask_key: "integration" },
    ];
    value.collaboration.input_groups[0] = {
      ...value.collaboration.input_groups[0],
      consumer_worker_item_key: "worker-integration",
      candidates: [
        { worker_item_key: "worker-source-a", priority: 1 },
        { worker_item_key: "worker-source-b", priority: 2 },
      ],
    };

    expect(() => assertClosureSampleProposal(value)).not.toThrow();
  });

  it("accepts source keys with the worker suffix used by the real planner", () => {
    const value = proposal();
    value.display.worker_items = [
      { key: "source-a-worker", subtask_key: "implementation" },
      { key: "source-b-worker", subtask_key: "implementation" },
      { key: "integration-worker", subtask_key: "integration" },
    ];
    value.collaboration.input_groups[0] = {
      ...value.collaboration.input_groups[0],
      consumer_worker_item_key: "integration-worker",
      candidates: [
        { worker_item_key: "source-a-worker", priority: 1 },
        { worker_item_key: "source-b-worker", priority: 2 },
      ],
    };

    expect(() => assertClosureSampleProposal(value)).not.toThrow();
  });

  it("accepts distinct integration groups that partition the required source workers", () => {
    const value = proposal();
    const base = value.collaboration.input_groups[0]!;
    const requirements = base.requirements as Record<string, unknown>;
    value.collaboration.input_groups = [
      {
        ...base,
        group_key: "source-a-input",
        candidates: [{ worker_item_key: "source-a", priority: 1 }],
        requirements: {
          ...requirements,
          required_artifact_refs: ["artifact_type:diff"],
          required_evidence_refs: ["acceptance:0000"],
        },
      },
      {
        ...base,
        group_key: "source-b-input",
        candidates: [{ worker_item_key: "source-b", priority: 1 }],
        requirements: {
          ...requirements,
          required_artifact_refs: ["artifact_type:diff"],
          required_evidence_refs: ["acceptance:0001"],
        },
      },
    ];

    expect(() => assertClosureSampleProposal(value)).not.toThrow();
  });

  it.each([
    ["omits a required source", ["source-a"]],
    ["duplicates a required source", ["source-a", "source-a"]],
  ])("rejects partitioned groups that %s", (_caseName, sourceKeys) => {
    const value = proposal();
    const base = value.collaboration.input_groups[0]!;
    value.collaboration.input_groups = sourceKeys.map((sourceKey, index) => ({
      ...base,
      group_key: `source-${index}-input`,
      candidates: [{ worker_item_key: sourceKey, priority: 1 }],
    }));

    expect(() => assertClosureSampleProposal(value))
      .toThrow(/closure_sample_proposal_invalid/);
  });

  it("rejects source candidates that do not reference the actual prefixed WorkerItems", () => {
    const value = proposal();
    value.display.worker_items = [
      { key: "worker-source-a", subtask_key: "implementation" },
      { key: "worker-source-b", subtask_key: "implementation" },
      { key: "worker-integration", subtask_key: "integration" },
    ];
    value.collaboration.input_groups[0] = {
      ...value.collaboration.input_groups[0],
      consumer_worker_item_key: "worker-integration",
      candidates: [
        { worker_item_key: "source-a", priority: 1 },
        { worker_item_key: "worker-source-b", priority: 2 },
      ],
    };

    expect(() => assertClosureSampleProposal(value))
      .toThrow(/closure_sample_proposal_invalid/);
  });

  it.each([
    ["at_least with only one required source", { mode: "at_least", minimum_count: 1 }],
    ["a missing source candidate", {
      candidates: [{ worker_item_key: "source-a", priority: 1 }],
    }],
    ["a duplicate candidate", {
      candidates: [
        { worker_item_key: "source-a", priority: 1 },
        { worker_item_key: "source-a", priority: 2 },
      ],
    }],
    ["a wrong consumer", { consumer_worker_item_key: "source-a" }],
  ])("rejects %s", (_caseName, inputGroup) => {
    expect(() => assertClosureSampleProposal(proposal(inputGroup)))
      .toThrow(/closure_sample_proposal_invalid/);
  });

  it("rejects an integration input requirement owned by the integration subtask", () => {
    const value = proposal({
      requirements: {
        execution_status: "succeeded",
        required_artifact_refs: ["artifact_type:report"],
        required_evidence_refs: ["acceptance:0002"],
        evidence_status: "candidate",
      },
    });

    expect(() => assertClosureSampleProposal(value))
      .toThrow(/collaboration.input_groups.integration.*requirements.required_evidence_refs/);
  });

  it("accepts empty or source-owned integration input evidence requirements", () => {
    const emptyEvidence = proposal();
    const sourceEvidence = proposal({
      requirements: {
        execution_status: "succeeded",
        required_artifact_refs: ["artifact_type:report"],
        required_evidence_refs: ["acceptance:0000"],
        evidence_status: "candidate",
      },
    });

    expect(() => assertClosureSampleProposal(emptyEvidence)).not.toThrow();
    expect(() => assertClosureSampleProposal(sourceEvidence)).not.toThrow();
  });

  it("accepts an integration input evidence ref shared with its source subtask", () => {
    const value = proposal({
      requirements: {
        execution_status: "succeeded",
        required_artifact_refs: ["artifact_type:report"],
        required_evidence_refs: ["acceptance:0000"],
        evidence_status: "candidate",
      },
    });
    value.display.subtasks[1] = {
      ...value.display.subtasks[1],
      acceptance_refs: ["acceptance:0000", "acceptance:0002", "acceptance:0003"],
    };

    expect(() => assertClosureSampleProposal(value)).not.toThrow();
  });

  it("rejects duplicate integration groups", () => {
    const value = proposal();
    const collaboration = value.collaboration;
    collaboration.input_groups.push({
      ...collaboration.input_groups[0],
      group_key: "second-integration-group",
    });

    expect(() => assertClosureSampleProposal(value))
      .toThrow(/closure_sample_proposal_invalid/);
  });

  it("rejects incorrect worker-to-subtask placement", () => {
    const value = proposal();
    value.display.worker_items[1] = {
      ...value.display.worker_items[1],
      subtask_key: "integration",
    };

    expect(() => assertClosureSampleProposal(value))
      .toThrow(/closure_sample_proposal_invalid/);
  });

  it("rejects a duplicate worker key that replaces a required source", () => {
    const value = proposal();
    value.display.worker_items[1] = {
      ...value.display.worker_items[1],
      key: "source-a",
    };

    expect(() => assertClosureSampleProposal(value))
      .toThrow(/closure_sample_proposal_invalid/);
  });

  it("accepts selected and persisted results from both required sources", () => {
    expect(() => assertClosureSampleIntegrationInputs(integrationFacts())).not.toThrow();
  });

  it("locates integration input groups when their source keys include the worker prefix", () => {
    const value = integrationFacts();
    const selectedSources = [
      { worker_item_key: "worker-source-a", priority: 1, effective_execution_id: "execution-a" },
      { worker_item_key: "worker-source-b", priority: 2, effective_execution_id: "execution-b" },
    ];
    const group = value.resultInputs.groups[0]!;
    group.candidates = [
      { worker_item_key: "worker-source-a", priority: 1 },
      { worker_item_key: "worker-source-b", priority: 2 },
    ];
    group.selected_sources = selectedSources;
    value.selection.groups = [{
      group_key: "planner-chosen-key",
      sources: selectedSources,
    }];
    expect(findClosureSampleIntegrationInputGroups(value.resultInputs)).toEqual([group]);
    expect(() => assertClosureSampleIntegrationInputs(value)).not.toThrow();
  });

  it("validates persisted selections when runtime groups partition the two sources", () => {
    const value = integrationFacts();
    const aggregate = value.resultInputs.groups[0]!;
    const selectedSources = aggregate.selected_sources as Record<string, unknown>[];
    const candidates = aggregate.candidates as Record<string, unknown>[];
    const splitGroups = [
      {
        ...aggregate,
        group_key: "source-a-input",
        required_count: 1,
        candidates: [candidates[0]!],
        selected_sources: [selectedSources[0]!],
      },
      {
        ...aggregate,
        group_key: "source-b-input",
        required_count: 1,
        candidates: [{ ...candidates[1]!, priority: 1 }],
        selected_sources: [{ ...selectedSources[1]!, priority: 1 }],
      },
    ];
    value.resultInputs.groups = splitGroups;
    value.selection.groups = splitGroups.map((group) => ({
      group_key: group.group_key,
      sources: group.selected_sources,
    }));

    expect(findClosureSampleIntegrationInputGroups(value.resultInputs)).toEqual(splitGroups);
    expect(() => assertClosureSampleIntegrationInputs(value)).not.toThrow();
  });

  it("rejects an integration selection that contains only one source", () => {
    const value = integrationFacts();
    const group = value.resultInputs.groups[0]!;
    group.selected_sources = (group.selected_sources as unknown[]).slice(0, 1);

    expect(() => assertClosureSampleIntegrationInputs(value))
      .toThrow(/closure_sample_result_inputs_invalid/);
  });

  it("rejects a runtime input group that requires only one source", () => {
    const value = integrationFacts();
    const group = value.resultInputs.groups[0]!;
    group.mode = "at_least";
    group.minimum_count = 1;
    group.required_count = 1;

    expect(() => assertClosureSampleIntegrationInputs(value))
      .toThrow(/closure_sample_result_inputs_invalid/);
  });

  it("rejects source candidates whose priority order differs from the brief", () => {
    const value = integrationFacts();
    const group = value.resultInputs.groups[0]!;
    group.candidates = (group.candidates as Record<string, unknown>[]).reverse();

    expect(() => assertClosureSampleIntegrationInputs(value))
      .toThrow(/closure_sample_result_inputs_invalid/);
  });

  it("rejects a candidate whose selected source executions do not match", () => {
    const value = integrationFacts();
    const sources = value.candidateDetail.source_results as Record<string, unknown>[];
    sources[1] = { ...sources[1], execution_id: "unselected-execution" };

    expect(() => assertClosureSampleIntegrationInputs(value))
      .toThrow(/closure_sample_result_inputs_invalid/);
  });

  it.each([
    ["a missing source result", ["snapshot-a"]],
    ["a duplicated source result", ["snapshot-a", "snapshot-a"]],
  ])("rejects a candidate with %s", (_caseName, sourceResultIds) => {
    const value = integrationFacts();
    const candidate = value.resultOverview.current_candidate as Record<string, unknown>;
    candidate.source_result_ids = sourceResultIds;

    expect(() => assertClosureSampleIntegrationInputs(value))
      .toThrow(/closure_sample_result_inputs_invalid/);
  });

  it("rejects a candidate that is not stored", () => {
    const value = integrationFacts();
    const detailCandidate = value.candidateDetail.candidate as Record<string, unknown>;
    detailCandidate.storage_status = "missing";

    expect(() => assertClosureSampleIntegrationInputs(value))
      .toThrow(/closure_sample_result_inputs_invalid/);
  });
});

describe("closure sample integration spec", () => {
  it("accepts a planner-chosen automatic criterion key linked to the sole closure check", () => {
    const value = {
      display: {
        integration_spec: {
          checks: [{ key: "closure" }],
          criteria: [{
            key: "closure-integration",
            mode: "automatic",
            check_keys: ["closure"],
          }],
        },
      },
    };

    expect(() => assertClosureSampleIntegrationSpec(value, "closure")).not.toThrow();
  });

  it("rejects an automatic criterion that does not cover the closure check", () => {
    const value = {
      display: {
        integration_spec: {
          checks: [{ key: "closure" }],
          criteria: [{
            key: "closure-integration",
            mode: "automatic",
            check_keys: ["patch-integrity"],
          }],
        },
      },
    };

    expect(() => assertClosureSampleIntegrationSpec(value, "closure"))
      .toThrow(/closure_sample_proposal_invalid/);
  });
});
