import { ArrowDown, Plus, ShieldAlert } from "lucide-react";

import type { ProposalViewModel } from "./proposalTypes";

export function ProposalDiff({ model }: { model: ProposalViewModel }) {
  const workersByKey = new Map(model.workerItems.map((worker) => [worker.key, worker]));
  return (
    <section className="planning-proposal__diff" aria-labelledby="proposal-diff-title">
      <div className="planning-section-heading">
        <div>
          <span className="eyebrow">PROPOSAL DIFF</span>
          <h2 id="proposal-diff-title">变更</h2>
        </div>
        <span className="mono-label">相对空白任务基线</span>
      </div>
      <div className="planning-proposal__diff-stats">
        {model.diff.rootTaskAdded ? <DiffStat icon={<Plus size={14} aria-hidden="true" />} label="新增 Root Task" value={model.rootTask.title} /> : null}
        <DiffStat icon={<Plus size={14} aria-hidden="true" />} label={`新增 ${model.diff.addedSubtaskKeys.length} 个子任务`} value={model.subtasks.map((item) => item.title).join("、") || "无"} />
        <DiffStat icon={<Plus size={14} aria-hidden="true" />} label={`新增 ${model.diff.addedWorkerItemKeys.length} 个 WorkerItem`} value={model.workerItems.map((item) => item.title).join("、") || "无"} />
      </div>
      <div className="planning-proposal__readiness-list">
        <div>
          <span className="eyebrow">INITIAL GATE</span>
          <h3>确认后的首批执行状态</h3>
        </div>
        {model.diff.immediatelyReadyWorkerItemKeys.map((key) => {
          const worker = workersByKey.get(key);
          return <p className="planning-proposal__diff-row planning-proposal__diff-row--ready" key={key}><ArrowDown size={14} aria-hidden="true" />已就绪：{worker?.title ?? key}</p>;
        })}
        {model.diff.blockedWorkerItemKeys.map((key) => {
          const worker = workersByKey.get(key);
          return <p className="planning-proposal__diff-row planning-proposal__diff-row--blocked" key={key}><ShieldAlert size={14} aria-hidden="true" />等待依赖：{worker?.title ?? key}</p>;
        })}
        {model.diff.immediatelyReadyWorkerItemKeys.length === 0 && model.diff.blockedWorkerItemKeys.length === 0 ? <p className="planning-proposal__empty">规划器没有提供首批 Gate 预测。</p> : null}
      </div>
      {model.diff.addedSubtaskDependencyEdges.length > 0 ? (
        <div className="planning-proposal__diff-dependencies">
          <span className="eyebrow">DEPENDENCIES</span>
          {model.diff.addedSubtaskDependencyEdges.map((edge) => <span key={edge.join(":")}>{edge[0]} → {edge[1]}</span>)}
        </div>
      ) : null}
    </section>
  );
}

function DiffStat({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="planning-proposal__diff-stat">
      <span>{icon}{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
