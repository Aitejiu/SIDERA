import { ChevronDown, ChevronRight, GitBranch, Layers3, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";

import type { ProposalViewModel, ProposalWorkerItem } from "./proposalTypes";

export function ProposalArchitectureBoard({ model }: { model: ProposalViewModel }) {
  const [expandedSubtasks, setExpandedSubtasks] = useState<Set<string>>(new Set());
  const [expandedWorkers, setExpandedWorkers] = useState<Set<string>>(new Set());
  const workersByKey = useMemo(() => new Map(model.workerItems.map((worker) => [worker.key, worker])), [model.workerItems]);

  return (
    <section className="planning-proposal__architecture" aria-labelledby="proposal-architecture-title">
      <div className="planning-section-heading">
        <div>
          <span className="eyebrow">TASK FIRST / EXECUTION SHAPE</span>
          <h2 id="proposal-architecture-title">执行架构</h2>
        </div>
        <span className="mono-label">{model.subtasks.length} 个子任务 · {model.workerItems.length} 个 WorkerItem</span>
      </div>
      <div className="planning-proposal__root-line">
        <div>
          <span className="eyebrow">ROOT TASK</span>
          <strong>{model.rootTask.title}</strong>
          <p>{model.rootTask.goalSummary}</p>
        </div>
        <span className="planning-proposal__root-state"><ShieldCheck size={14} aria-hidden="true" />确认后创建</span>
      </div>
      <div className="planning-proposal__subtasks">
        {model.subtasks.map((subtask) => {
          const workers = model.workerItems.filter((worker) => worker.subtaskKey === subtask.key);
          const expanded = expandedSubtasks.has(subtask.key);
          return (
            <article className="planning-proposal__subtask" key={subtask.key}>
              <button
                className="planning-proposal__subtask-toggle"
                type="button"
                aria-expanded={expanded}
                aria-controls={`proposal-subtask-${subtask.key}`}
                aria-label={`${expanded ? "收起" : "展开"}子任务：${subtask.title}`}
                onClick={() => toggle(expandedSubtasks, setExpandedSubtasks, subtask.key)}
              >
                {expanded ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
                <span className="planning-proposal__subtask-title">
                  <strong>{subtask.title}</strong>
                  <span>{subtask.goalSummary}</span>
                </span>
                <span className="planning-proposal__subtask-meta">
                  <span className={`planning-proposal__readiness planning-proposal__readiness--${subtaskReadiness(workers)}`}>{subtaskReadinessLabel(workers)}</span>
                  <span>{workers.length} WorkerItem</span>
                </span>
              </button>
              {expanded ? (
                <div className="planning-proposal__subtask-body" id={`proposal-subtask-${subtask.key}`}>
                  <div className="planning-proposal__subtask-facts">
                    <Fact label="验收" items={subtask.acceptanceRefs} />
                    <Fact label="产物" items={subtask.expectedOutputs} />
                  </div>
                </div>
              ) : null}
              {workers.length > 0 ? (
                <div className="planning-proposal__worker-list">
                  {workers.map((worker) => {
                    const workerExpanded = expandedWorkers.has(worker.key);
                    return (
                      <div className="planning-proposal__worker" key={worker.key}>
                        <button
                          className="planning-proposal__worker-toggle"
                          type="button"
                          aria-expanded={workerExpanded}
                          onClick={() => toggle(expandedWorkers, setExpandedWorkers, worker.key)}
                        >
                          {workerExpanded ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                          <span className="planning-proposal__worker-main">
                            <strong>{worker.title}</strong>
                            <span>{worker.responsibility}</span>
                          </span>
                          <span className={`planning-proposal__readiness planning-proposal__readiness--${worker.readiness}`}>
                            {workerReadinessLabel(worker.readiness)}
                          </span>
                        </button>
                        <div className="planning-proposal__worker-chips" aria-label={`${worker.title} 能力要求`}>
                          <span className="planning-proposal__role"><Layers3 size={12} aria-hidden="true" />{worker.role}</span>
                          {worker.capabilityRequirements.map((capability) => <span key={capability}>{capability}</span>)}
                        </div>
                        {dependencySummary(worker, model, workersByKey)}
                        {workerExpanded ? <WorkerDetails worker={worker} /> : null}
                      </div>
                    );
                  })}
                </div>
              ) : <p className="planning-proposal__empty">这个子任务还没有 WorkerItem。</p>}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function WorkerDetails({ worker }: { worker: ProposalWorkerItem }) {
  return (
    <dl className="planning-proposal__worker-details">
      <div><dt>职责</dt><dd>{worker.responsibility}</dd></div>
      <div><dt>策略</dt><dd>{worker.workerPolicyRef ?? "沿用默认策略"}</dd></div>
      <div><dt>配置</dt><dd>{worker.configurationRef ?? "沿用 Worker 配置"}</dd></div>
      <div><dt>并发</dt><dd>{worker.concurrencyLimit ?? "未限制"}</dd></div>
    </dl>
  );
}

function Fact({ label, items }: { label: string; items: string[] }) {
  return (
    <div>
      <span>{label}</span>
      {items.length > 0 ? <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>未声明</p>}
    </div>
  );
}

function dependencySummary(
  worker: ProposalWorkerItem,
  model: ProposalViewModel,
  workersByKey: Map<string, ProposalWorkerItem>,
) {
  const upstream = model.workerItemDependencies.filter((edge) => edge.downstreamWorkerItemKey === worker.key);
  const downstream = model.workerItemDependencies.filter((edge) => edge.upstreamWorkerItemKey === worker.key);
  if (upstream.length === 0 && downstream.length === 0) return null;
  return (
    <div className="planning-proposal__worker-dependencies">
      <GitBranch size={12} aria-hidden="true" />
      {upstream.length > 0 ? <span>前置：{upstream.map((edge) => workersByKey.get(edge.upstreamWorkerItemKey)?.title ?? edge.upstreamWorkerItemKey).join("、")}</span> : null}
      {downstream.length > 0 ? <span>产出给：{downstream.map((edge) => workersByKey.get(edge.downstreamWorkerItemKey)?.title ?? edge.downstreamWorkerItemKey).join("、")}</span> : null}
    </div>
  );
}

function subtaskReadiness(workers: ProposalWorkerItem[]): ProposalWorkerItem["readiness"] {
  if (workers.some((worker) => worker.readiness === "blocked")) return "blocked";
  if (workers.length > 0 && workers.every((worker) => worker.readiness === "ready")) return "ready";
  return "unknown";
}

function subtaskReadinessLabel(workers: ProposalWorkerItem[]): string {
  const readiness = subtaskReadiness(workers);
  return readiness === "ready" ? "预计可启动" : readiness === "blocked" ? "等待依赖" : "待确认";
}

function workerReadinessLabel(readiness: ProposalWorkerItem["readiness"]): string {
  return readiness === "ready" ? "预计就绪" : readiness === "blocked" ? "等待依赖" : "待确认";
}

function toggle(
  current: Set<string>,
  setState: (next: Set<string>) => void,
  key: string,
) {
  const next = new Set(current);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  setState(next);
}
