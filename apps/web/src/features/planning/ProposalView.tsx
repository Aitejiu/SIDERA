import { useMemo, useState } from "react";

import type { PlanningProposal } from "./types";
import { ProposalActions, type ProposalReviewBinding } from "./ProposalActions";
import { ProposalArchitectureBoard } from "./ProposalArchitectureBoard";
import { ProposalDecisionSummary } from "./ProposalDecisionSummary";
import { ProposalDiff } from "./ProposalDiff";
import { ProposalGraph } from "./ProposalGraph";
import { normalizePlanningProposal } from "./proposalTypes";

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
      <div className="planning-proposal__view-switch" role="tablist" aria-label="方案视图">
        <ViewButton active={mode === "architecture"} label="执行架构" onClick={() => setMode("architecture")} />
        <ViewButton active={mode === "graph"} label="依赖 DAG" onClick={() => setMode("graph")} />
        <ViewButton active={mode === "diff"} label="变更" onClick={() => setMode("diff")} />
      </div>
      {mode === "architecture" ? <ProposalArchitectureBoard model={model} /> : null}
      {mode === "graph" ? <ProposalGraph model={model} /> : null}
      {mode === "diff" ? <ProposalDiff model={model} /> : null}
      <ProposalActions
        binding={binding}
        onConfirm={onConfirm}
        {...(onRevisit === undefined ? {} : { onRevisit })}
        pending={pending}
      />
    </section>
  );
}

function ViewButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button type="button" role="tab" aria-selected={active} className={active ? "is-active" : ""} onClick={onClick}>
      {label}
    </button>
  );
}
