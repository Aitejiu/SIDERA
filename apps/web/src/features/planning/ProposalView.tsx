import { useMemo, useState } from "react";

import type { PlanningProposal } from "./types";
import { ProposalActions, type ProposalReviewBinding } from "./ProposalActions";
import { ProposalArchitectureBoard } from "./ProposalArchitectureBoard";
import { ProposalDecisionSummary } from "./ProposalDecisionSummary";
import { ProposalDiff } from "./ProposalDiff";
import { ProposalGraph } from "./ProposalGraph";
import { ProposalInputGroups } from "./ProposalInputGroups";
import { normalizePlanningProposal } from "./proposalTypes";
import { ResultPlanSummary } from "@/features/results/ResultPlanSummary";

type ProposalViewMode = "architecture" | "graph" | "diff";

export function ProposalView({
  proposal,
  requestVersion,
  profileId,
  profileVersion,
  onConfirm,
  onRevisit,
  pending = false,
}: {
  proposal: PlanningProposal;
  requestVersion: number;
  profileId: string;
  profileVersion: number;
  onConfirm: (binding: ProposalReviewBinding) => void | Promise<void>;
  onRevisit?: () => void;
  pending?: boolean;
}) {
  const [mode, setMode] = useState<ProposalViewMode>("architecture");
  const model = useMemo(() => normalizePlanningProposal(proposal), [proposal]);
  const binding: ProposalReviewBinding = {
    requestVersion,
    proposalRevision: proposal.revision,
    proposalContentHash: proposal.content_hash,
    profileId,
    profileVersion,
  };
  return (
    <section className="planning-proposal" aria-labelledby="planning-proposal-title">
      <div className="planning-proposal__heading">
        <div>
          <span className="eyebrow">PROPOSAL REVIEW</span>
          <h2 id="planning-proposal-title">方案待确认</h2>
          <p>先看目标和拆分，再按需要检查依赖关系或变更摘要。</p>
        </div>
        <span className="mono-label">Proposal V{proposal.revision} · Brief V{proposal.brief_revision}</span>
      </div>
      <ProposalDecisionSummary summary={model.decisionSummary} />
      <ResultPlanSummary
        proposalSchemaVersion={proposal.proposal_schema_version}
        integrationSpec={model.displayIntegrationSpec}
      />
      <div className="planning-proposal__view-switch" role="tablist" aria-label="方案视图">
        <ViewButton mode="architecture" active={mode === "architecture"} label="执行架构" onClick={() => setMode("architecture")} />
        <ViewButton mode="graph" active={mode === "graph"} label="依赖 DAG" onClick={() => setMode("graph")} />
        <ViewButton mode="diff" active={mode === "diff"} label="变更" onClick={() => setMode("diff")} />
      </div>
      <div
        id={`proposal-panel-${mode}`}
        role="tabpanel"
        aria-labelledby={`proposal-tab-${mode}`}
        tabIndex={0}
      >
        {mode === "architecture" ? (
          <>
            <ProposalInputGroups groups={model.inputGroups} />
            <ProposalArchitectureBoard model={model} />
          </>
        ) : null}
        {mode === "graph" ? <ProposalGraph model={model} /> : null}
        {mode === "diff" ? <ProposalDiff model={model} /> : null}
      </div>
      <ProposalActions
        binding={binding}
        onConfirm={onConfirm}
        {...(onRevisit === undefined ? {} : { onRevisit })}
        pending={pending}
      />
    </section>
  );
}

function ViewButton({
  mode,
  active,
  label,
  onClick,
}: {
  mode: ProposalViewMode;
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      id={`proposal-tab-${mode}`}
      type="button"
      role="tab"
      aria-selected={active}
      aria-controls={`proposal-panel-${mode}`}
      tabIndex={active ? 0 : -1}
      className={active ? "is-active" : ""}
      onClick={onClick}
    >
      {label}
    </button>
  );
}
