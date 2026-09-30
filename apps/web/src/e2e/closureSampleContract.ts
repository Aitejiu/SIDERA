const SOURCE_KEYS = ["source-a", "source-b"] as const;
type SourceKey = typeof SOURCE_KEYS[number];

type ProposalWorkerEntry = {
  key: string;
  subtaskKey: string;
  sourceKey: SourceKey | null;
  responsibility: unknown;
};

export function closureSampleAcceptanceMarkerInstruction(marker: string): string {
  if (marker.trim().length === 0) {
    throw new Error("closure_sample_marker_instruction_invalid: required_marker");
  }
  return `In out/summary.json, include this exact run marker as a top-level JSON string property "acceptance_marker" whose value is ${JSON.stringify(marker)}.`;
}

export function closureSampleSourceKey(key: string): SourceKey | null {
  for (const sourceKey of SOURCE_KEYS) {
    if (
      key === sourceKey
      || key === `worker-${sourceKey}`
      || key === `${sourceKey}-worker`
    ) return sourceKey;
  }
  return null;
}

export function closureSampleWorkerItemSourceKey(worker: {
  stableKey: string;
  title: string;
  responsibility?: string;
}): SourceKey | null {
  const stableKey = closureSampleSourceKey(worker.stableKey);
  const title = worker.title.trim();
  const titleKey = SOURCE_KEYS.find((sourceKey) => {
    if (!title.startsWith(sourceKey)) return false;
    const suffix = title.slice(sourceKey.length);
    return suffix.length === 0 || /^[\s:_-]/u.test(suffix);
  }) ?? null;
  const responsibilityKeys = worker.responsibility === undefined
    ? []
    : [...worker.responsibility.matchAll(
      /(?:^|[^A-Za-z0-9_.-])out\/(source-a|source-b)\.json(?:$|[^A-Za-z0-9_.-])/gu,
    )].map((match) => match[1] as SourceKey);
  const responsibilityKey = new Set(responsibilityKeys).size === 1
    ? responsibilityKeys[0] ?? null
    : null;
  const identities = [stableKey, titleKey, responsibilityKey]
    .filter((key): key is SourceKey => key !== null);
  if (new Set(identities).size > 1 || responsibilityKeys.length > 0 && responsibilityKey === null) {
    return null;
  }
  return identities[0] ?? null;
}

export function assertClosureSampleIntegrationSpec(
  proposal: unknown,
  expectedCheckKey: string,
): void {
  const proposalRecord = record(proposal, "proposal");
  const display = record(proposalRecord.display, "display");
  const integrationSpec = record(display.integration_spec, "display.integration_spec");
  const checks = array(integrationSpec.checks, "display.integration_spec.checks")
    .map((item, index) => record(item, `display.integration_spec.checks[${index}]`));
  if (checks.length !== 1 || checks[0]?.key !== expectedCheckKey) {
    invalid("display.integration_spec.checks");
  }

  const criteria = array(integrationSpec.criteria, "display.integration_spec.criteria")
    .map((item, index) => record(item, `display.integration_spec.criteria[${index}]`));
  const matchingCriteria = criteria.filter((criterion) => (
    criterion.mode === "automatic"
    && isExactStringList(criterion.check_keys, [expectedCheckKey])
  ));
  if (matchingCriteria.length !== 1) {
    invalid("display.integration_spec.criteria");
  }
  string(matchingCriteria[0]?.key, "display.integration_spec.criteria.key");
}

export function assertClosureSampleProposal(
  proposal: unknown,
  requiredMarker?: string,
): void {
  const proposalRecord = record(proposal, "proposal");
  const display = record(proposalRecord.display, "display");
  const subtasks = array(display.subtasks, "display.subtasks");
  const subtaskEntries = subtasks.map((item, index) => {
    const path = `display.subtasks[${index}]`;
    const subtask = record(item, path);
    return {
      key: string(subtask.key, `${path}.key`),
      acceptanceRefs: array(subtask.acceptance_refs, `${path}.acceptance_refs`)
        .map((ref, refIndex) => string(ref, `${path}.acceptance_refs[${refIndex}]`)),
    };
  });
  const subtaskKeys = subtaskEntries.map(({ key }) => key);
  if (subtaskKeys.length !== 2 || new Set(subtaskKeys).size !== 2) {
    invalid("display.subtasks");
  }

  const workers = array(display.worker_items, "display.worker_items");
  const workerEntries = workers.map((item, index) => {
    const worker = record(item, `display.worker_items[${index}]`);
    const key = string(worker.key, `display.worker_items[${index}].key`);
    return {
      key,
      subtaskKey: string(worker.subtask_key, `display.worker_items[${index}].subtask_key`),
      sourceKey: closureSampleSourceKey(key),
      responsibility: worker.responsibility,
    };
  });
  const sourceWorkers = workerEntries.filter(
    (entry): entry is ProposalWorkerEntry & { sourceKey: SourceKey } => entry.sourceKey !== null,
  );
  const integrationWorkers = workerEntries.filter(({ sourceKey }) => sourceKey === null);
  const sourceSubtask = sourceWorkers[0]?.subtaskKey;
  const integrationWorker = integrationWorkers[0];
  const integrationSubtask = integrationWorker?.subtaskKey;
  const populatedSubtasks = [...new Set(workerEntries.map(({ subtaskKey }) => subtaskKey))];
  const acceptanceRefsBySubtask = new Map(
    subtaskEntries.map(({ key, acceptanceRefs }) => [key, new Set(acceptanceRefs)]),
  );
  const sourceWorkerKeys = new Map(
    sourceWorkers.map(({ sourceKey, key }) => [sourceKey, key]),
  );
  if (workerEntries.length !== SOURCE_KEYS.length + 1
    || new Set(workerEntries.map(({ key }) => key)).size !== workerEntries.length
    || !sameSet(sourceWorkers.map(({ sourceKey }) => sourceKey), SOURCE_KEYS)
    || integrationWorkers.length !== 1
    || !sameSet(populatedSubtasks, subtaskKeys)
    || sourceSubtask === undefined
    || sourceWorkers.some(({ subtaskKey }) => subtaskKey !== sourceSubtask)
    || integrationWorker === undefined
    || integrationSubtask === undefined
    || integrationSubtask === sourceSubtask) {
    invalid("display.worker_items");
  }

  if (requiredMarker !== undefined) {
    if (requiredMarker.trim().length === 0) invalid("required_marker");
    const responsibility = string(
      integrationWorker.responsibility,
      "display.worker_items.integration.responsibility",
    );
    const integrationSpec = record(display.integration_spec, "display.integration_spec");
    const criteria = array(integrationSpec.criteria, "display.integration_spec.criteria");
    const matchingCriteria = criteria.filter((item, index) => (
      record(item, `display.integration_spec.criteria[${index}]`).key === "integration"
    ));
    if (matchingCriteria.length !== 1) {
      invalid("display.integration_spec.criteria.integration");
    }
    const criterionText = string(
      record(matchingCriteria[0], "display.integration_spec.criteria.integration").text,
      "display.integration_spec.criteria.integration.text",
    );
    if (!responsibility.includes(requiredMarker) || !criterionText.includes(requiredMarker)) {
      invalid("display.integration_spec.criteria.integration.required_marker");
    }
  }

  const collaboration = record(proposalRecord.collaboration, "collaboration");
  const inputGroups = array(collaboration.input_groups, "collaboration.input_groups");
  const integrationGroups = inputGroups.filter((item, index) => (
    record(item, `collaboration.input_groups[${index}]`).consumer_worker_item_key === integrationWorker.key
  ));
  if (integrationGroups.length === 0) invalid("collaboration.input_groups.integration");

  const seenGroupKeys = new Set<string>();
  const coveredSourceKeys: SourceKey[] = [];
  integrationGroups.forEach((item, groupIndex) => {
    const path = `collaboration.input_groups.integration[${groupIndex}]`;
    const group = record(item, path);
    const groupKey = string(group.group_key, `${path}.group_key`);
    if (seenGroupKeys.has(groupKey)) invalid(`${path}.group_key`);
    seenGroupKeys.add(groupKey);

    const requirements = record(group.requirements, `${path}.requirements`);
    array(
      requirements.required_artifact_refs,
      `${path}.requirements.required_artifact_refs`,
    ).forEach((ref, index) => string(
      ref,
      `${path}.requirements.required_artifact_refs[${index}]`,
    ));
    const requiredEvidenceRefs = array(
      requirements.required_evidence_refs,
      `${path}.requirements.required_evidence_refs`,
    ).map((ref, index) => string(
      ref,
      `${path}.requirements.required_evidence_refs[${index}]`,
    ));
    const integrationAcceptanceRefs = acceptanceRefsBySubtask.get(integrationSubtask);
    const sourceAcceptanceRefs = acceptanceRefsBySubtask.get(sourceSubtask);
    if (requiredEvidenceRefs.some((ref) =>
      integrationAcceptanceRefs?.has(ref) && !sourceAcceptanceRefs?.has(ref))) {
      invalid(`${path}.requirements.required_evidence_refs`);
    }

    const candidates = array(group.candidates, `${path}.candidates`);
    const candidateEntries = candidates.map((candidateValue, index) => {
      const candidate = record(candidateValue, `${path}.candidates[${index}]`);
      const key = string(candidate.worker_item_key, `${path}.candidates[${index}].worker_item_key`);
      return {
        key,
        sourceKey: closureSampleSourceKey(key),
        priority: candidate.priority,
      };
    });
    if (candidateEntries.length === 0
      || new Set(candidateEntries.map(({ key }) => key)).size !== candidateEntries.length
      || candidateEntries.some(({ key, sourceKey, priority }, index) => (
        sourceKey === null
        || key !== sourceWorkerKeys.get(sourceKey)
        || priority !== index + 1
      ))) {
      invalid(`${path}.candidates`);
    }

    const mode = group.mode;
    const minimumCount = group.minimum_count;
    if (!((mode === "all" && minimumCount === null)
      || (mode === "at_least" && minimumCount === candidateEntries.length))) {
      invalid(`${path}.mode`);
    }

    for (const candidate of candidateEntries) {
      if (candidate.sourceKey === null) invalid(`${path}.candidates`);
      coveredSourceKeys.push(candidate.sourceKey);
    }
  });
  if (coveredSourceKeys.length !== SOURCE_KEYS.length
    || new Set(coveredSourceKeys).size !== SOURCE_KEYS.length
    || !sameSet(coveredSourceKeys, SOURCE_KEYS)) {
    invalid("collaboration.input_groups.integration.candidates");
  }
}

export function findClosureSampleIntegrationInputGroups(
  resultInputsValue: unknown,
): Record<string, unknown>[] | null {
  const resultInputs = resultRecord(resultInputsValue, "result_inputs");
  const groups = resultArray(resultInputs.groups, "result_inputs.groups");
  if (groups.length === 0) return null;
  return groups.map((item, index) => (
    resultRecord(item, `result_inputs.groups[${index}]`)
  ));
}

export function assertClosureSampleIntegrationInputs(facts: {
  resultInputs: unknown;
  selection: unknown;
  resultOverview: unknown;
  candidateDetail: unknown;
}): void {
  const inputGroups = findClosureSampleIntegrationInputGroups(facts.resultInputs);
  if (inputGroups === null) resultInvalid("result_inputs.groups.integration");
  const selection = resultRecord(facts.selection, "selection");
  const selectionGroups = resultArray(selection.groups, "selection.groups");
  if (selectionGroups.length !== inputGroups.length) resultInvalid("selection.groups.integration");
  const selectionId = resultString(selection.selection_id, "selection.selection_id");
  const selectionHash = resultString(selection.selection_hash, "selection.selection_hash");
  const seenGroupKeys = new Set<string>();
  const coveredSourceKeys: SourceKey[] = [];
  const selectedBySource = new Map<SourceKey, ReturnType<typeof resultSources>[number]>();

  inputGroups.forEach((inputGroup, groupIndex) => {
    const path = `result_inputs.groups[${groupIndex}]`;
    const groupKey = resultString(inputGroup.group_key, `${path}.group_key`);
    if (seenGroupKeys.has(groupKey)) resultInvalid(`${path}.group_key`);
    seenGroupKeys.add(groupKey);
    if (inputGroup.selection_id !== selectionId || inputGroup.selection_hash !== selectionHash) {
      resultInvalid(`${path}.selection_identity`);
    }

    const runtimeCandidates = resultArray(
      inputGroup.candidates,
      `${path}.candidates`,
    ).map((item, index) => {
      const candidate = resultRecord(item, `${path}.candidates[${index}]`);
      const key = resultString(candidate.worker_item_key, `${path}.candidates[${index}].worker_item_key`);
      return {
        key,
        sourceKey: closureSampleSourceKey(key),
        priority: candidate.priority,
      };
    });
    if (runtimeCandidates.length === 0
      || new Set(runtimeCandidates.map(({ key }) => key)).size !== runtimeCandidates.length
      || runtimeCandidates.some(({ sourceKey, priority }, index) => (
        sourceKey === null || priority !== index + 1
      ))
      || inputGroup.required_count !== runtimeCandidates.length
      || !((inputGroup.mode === "all" && inputGroup.minimum_count === null)
        || (inputGroup.mode === "at_least" && inputGroup.minimum_count === runtimeCandidates.length))) {
      resultInvalid(`${path}.rule`);
    }

    const selectedSources = resultSources(
      inputGroup.selected_sources,
      `${path}.selected_sources`,
    );
    const candidateKeys = runtimeCandidates.map(({ key }) => key);
    if (!sameOrdered(
      selectedSources.map((source) => source.workerItemKey),
      candidateKeys,
    )) {
      resultInvalid(`${path}.selected_sources`);
    }

    const matchingSelections = selectionGroups.filter((item, index) => (
      resultRecord(item, `selection.groups[${index}]`).group_key === groupKey
    ));
    if (matchingSelections.length !== 1) resultInvalid("selection.groups.integration");
    const selectedGroup = resultRecord(matchingSelections[0], `selection.groups.${groupKey}`);
    const persistedSources = resultSources(selectedGroup.sources, `selection.groups.${groupKey}.sources`);
    if (!sameOrdered(
      persistedSources.map((source) => source.workerItemKey),
      candidateKeys,
    ) || persistedSources.some((source, index) => (
      source.executionId !== selectedSources[index]?.executionId
    ))) {
      resultInvalid(`selection.groups.${groupKey}.sources`);
    }

    for (let index = 0; index < runtimeCandidates.length; index += 1) {
      const candidate = runtimeCandidates[index]!;
      const source = selectedSources[index]!;
      if (candidate.sourceKey === null || selectedBySource.has(candidate.sourceKey)) {
        resultInvalid(`${path}.candidates`);
      }
      coveredSourceKeys.push(candidate.sourceKey);
      selectedBySource.set(candidate.sourceKey, source);
    }
  });
  if (coveredSourceKeys.length !== SOURCE_KEYS.length
    || new Set(coveredSourceKeys).size !== SOURCE_KEYS.length
    || !sameSet(coveredSourceKeys, SOURCE_KEYS)) {
    resultInvalid("result_inputs.groups.integration.candidates");
  }
  const selectedSources = SOURCE_KEYS.map((sourceKey) => {
    const source = selectedBySource.get(sourceKey);
    if (source === undefined) resultInvalid("result_inputs.groups.integration.selected_sources");
    return source;
  });
  if (new Set(selectedSources.map((source) => source.executionId)).size !== SOURCE_KEYS.length) {
    resultInvalid("result_inputs.groups.integration.selected_sources.execution_id");
  }

  const resultOverview = resultRecord(facts.resultOverview, "result_overview");
  const overviewCandidate = resultRecord(resultOverview.current_candidate, "result_overview.current_candidate");
  const candidateId = resultString(overviewCandidate.candidate_id, "result_overview.current_candidate.candidate_id");
  const sourceResultIds = uniqueStrings(
    overviewCandidate.source_result_ids,
    "result_overview.current_candidate.source_result_ids",
  );
  if (sourceResultIds.length !== SOURCE_KEYS.length || overviewCandidate.storage_status !== "available") {
    resultInvalid("result_overview.current_candidate");
  }

  const candidateDetail = resultRecord(facts.candidateDetail, "candidate_detail");
  const detailCandidate = resultRecord(candidateDetail.candidate, "candidate_detail.candidate");
  if (detailCandidate.candidate_id !== candidateId
    || detailCandidate.storage_status !== "available"
    || !sameSet(uniqueStrings(
      detailCandidate.source_result_ids,
      "candidate_detail.candidate.source_result_ids",
    ), sourceResultIds)) {
    resultInvalid("candidate_detail.candidate");
  }
  const sourceResults = resultArray(candidateDetail.source_results, "candidate_detail.source_results")
    .map((item, index) => resultRecord(item, `candidate_detail.source_results[${index}]`))
    .filter((item) => sourceResultIds.includes(
      resultString(item.snapshot_id, "candidate_detail.source_results.snapshot_id"),
    ));
  if (sourceResults.length !== SOURCE_KEYS.length) {
    resultInvalid("candidate_detail.source_results");
  }
  const candidateExecutions = sourceResults.map((source) => (
    resultString(source.execution_id, "candidate_detail.source_results.execution_id")
  ));
  if (!sameSet(candidateExecutions, selectedSources.map((source) => source.executionId))) {
    resultInvalid("candidate_detail.source_results.execution_id");
  }
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return invalid(path);
  }
  return value as Record<string, unknown>;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) return invalid(path);
  return value;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) return invalid(path);
  return value;
}

function sameSet(actual: string[], expected: readonly string[]): boolean {
  return actual.length === expected.length
    && new Set(actual).size === actual.length
    && expected.every((item) => actual.includes(item));
}

function sameOrdered(actual: string[], expected: readonly string[]): boolean {
  return actual.length === expected.length
    && actual.every((item, index) => item === expected[index]);
}

function isExactStringList(value: unknown, expected: readonly string[]): boolean {
  return Array.isArray(value)
    && value.every((item): item is string => typeof item === "string")
    && sameOrdered(value, expected);
}

function resultSources(value: unknown, path: string): { workerItemKey: string; executionId: string }[] {
  const sources = resultArray(value, path);
  return sources.map((item, index) => {
    const source = resultRecord(item, `${path}[${index}]`);
    return {
      workerItemKey: resultString(source.worker_item_key, `${path}[${index}].worker_item_key`),
      executionId: resultString(source.effective_execution_id, `${path}[${index}].effective_execution_id`),
    };
  });
}

function uniqueStrings(value: unknown, path: string): string[] {
  const values = resultArray(value, path).map((item, index) => (
    resultString(item, `${path}[${index}]`)
  ));
  if (new Set(values).size !== values.length) resultInvalid(path);
  return values;
}

function resultRecord(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return resultInvalid(path);
  }
  return value as Record<string, unknown>;
}

function resultArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) return resultInvalid(path);
  return value;
}

function resultString(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) return resultInvalid(path);
  return value;
}

function resultInvalid(path: string): never {
  throw new Error(`closure_sample_result_inputs_invalid: ${path}`);
}

function invalid(path: string): never {
  throw new Error(`closure_sample_proposal_invalid: ${path}`);
}
