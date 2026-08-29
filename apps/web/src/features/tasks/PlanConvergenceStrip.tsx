import type { ReactElement } from "react";
import { CircleAlert, CircleCheck, CircleDashed, GitBranch, LoaderCircle } from "lucide-react";

export type PlanConvergenceLike = {
  status: string;
  plan_transition_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  from_generation?: number | null;
  source_generation?: number | null;
  target_generation: number | null;
  total_count: number;
  converged_count: number;
  blocked_count: number;
  unknown_count: number;
  in_flight_count?: number;
  has_in_flight?: boolean;
  has_unknown?: boolean;
  has_active_fence?: boolean;
  confirmation_blocked?: boolean;
  blocked_predecessor_paths?: readonly (readonly string[])[];
};

type BlockedItem = {
  key: string;
  title: string;
  detail?: string | null;
};

export function PlanConvergenceStrip({
  convergence,
  blockedItems = [],
  label = "计划收敛",
}: {
  convergence: PlanConvergenceLike | null | undefined;
  blockedItems?: readonly BlockedItem[];
  label?: string;
}) {
  if (convergence === null || convergence === undefined) return null;

  const status = convergenceStatus(convergence.status);
  const progress = convergence.total_count > 0
    ? Math.min(100, Math.round((convergence.converged_count / convergence.total_count) * 100))
    : 0;
  const fromGeneration = convergence.from_generation ?? convergence.source_generation;
  const generationLabel = fromGeneration !== null && fromGeneration !== undefined
    && convergence.target_generation !== null
    ? `GEN ${fromGeneration} → GEN ${convergence.target_generation}`
    : convergence.target_generation === null
      ? "尚未产生目标代"
      : `GEN ${convergence.target_generation}`;
  const predecessorPaths = convergence.blocked_predecessor_paths ?? [];

  return (
    <section
      className={`plan-convergence-strip plan-convergence-strip--${status.kind}`}
      aria-labelledby={`plan-convergence-${label}`}
    >
      <div className="plan-convergence-strip__heading">
        <div>
          <span className="eyebrow"><GitBranch size={13} aria-hidden="true" />PLAN CONVERGENCE</span>
          <h2 id={`plan-convergence-${label}`}>{label}</h2>
        </div>
        <span className="plan-convergence-strip__status">
          {status.icon}
          {status.label}
        </span>
      </div>
      <div className="plan-convergence-strip__summary">
        <strong>{generationLabel}</strong>
        <strong>已收敛 {convergence.converged_count} / {convergence.total_count}</strong>
        {convergence.in_flight_count !== undefined ? <span>同步中 {convergence.in_flight_count}</span> : null}
        {convergence.blocked_count > 0 ? <span>阻塞 {convergence.blocked_count}</span> : null}
        {convergence.unknown_count > 0 ? <span>未知 {convergence.unknown_count}</span> : null}
      </div>
      <div className="plan-convergence-strip__progress" aria-label={`收敛进度 ${progress}%`}>
        <span style={{ width: `${progress}%` }} />
      </div>
      {blockedItems.length > 0 ? (
        <div className="plan-convergence-strip__issues">
          <span className="eyebrow">当前卡点</span>
          <div>
            {blockedItems.map((item) => (
              <div className="plan-convergence-strip__issue" key={item.key}>
                <CircleAlert size={14} aria-hidden="true" />
                <strong>{item.title}</strong>
                {item.detail ? <span>{item.detail}</span> : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {predecessorPaths.length > 0 ? (
        <div className="plan-convergence-strip__issues">
          <span className="eyebrow">阻塞前置路径</span>
          <div>
            {predecessorPaths.map((path) => (
              <div className="plan-convergence-strip__issue" key={path.join("/")}>
                <CircleDashed size={14} aria-hidden="true" />
                <span>{path.join(" / ")}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function convergenceStatus(status: string): {
  kind: "idle" | "running" | "completed" | "attention" | "waiting";
  label: string;
  icon: ReactElement;
} {
  if (status === "converged") return { kind: "completed", label: "已收敛", icon: <CircleCheck size={14} aria-hidden="true" /> };
  if (status === "blocked_unknown") return { kind: "attention", label: "未知结果", icon: <CircleAlert size={14} aria-hidden="true" /> };
  if (status === "degraded") return { kind: "attention", label: "部分失败", icon: <CircleAlert size={14} aria-hidden="true" /> };
  if (status === "waiting_dependency") return { kind: "waiting", label: "等待依赖", icon: <CircleDashed size={14} aria-hidden="true" /> };
  if (status === "converging") return { kind: "running", label: "同步中", icon: <LoaderCircle size={14} aria-hidden="true" /> };
  if (status === "waiting") return { kind: "waiting", label: "等待 Worker", icon: <CircleDashed size={14} aria-hidden="true" /> };
  return { kind: "idle", label: "尚未开始", icon: <CircleDashed size={14} aria-hidden="true" /> };
}
