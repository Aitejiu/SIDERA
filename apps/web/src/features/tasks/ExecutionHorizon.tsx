import { ArrowDown, Crosshair } from "lucide-react";

import { StatusMark } from "@/shared/ui/StatusMark";

import { statusKind } from "./TaskNode";
import type { TaskGraph } from "./types";

export function ExecutionHorizon({ graph }: { graph: TaskGraph }) {
  const nodes = graph.nodes.filter((node) => node.execution_horizon);
  return (
    <section className="execution-horizon" aria-labelledby="horizon-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">EXECUTION HORIZON</span>
          <h2 id="horizon-title">执行视野</h2>
        </div>
        <span className="mono-label">{nodes.length} 个相关节点</span>
      </div>
      <div className="horizon-list">
        {nodes.map((node, index) => (
          <div className="horizon-item" key={node.work_item_key}>
            {index > 0 ? <ArrowDown className="horizon-item__arrow" size={14} aria-hidden="true" /> : null}
            <Crosshair size={15} aria-hidden="true" />
            <div className="horizon-item__body">
              <strong>{node.title}</strong>
              <span className="mono-label">{node.work_item_key} · {node.critical_path ? "关键路径" : "直接关联"}</span>
            </div>
            <StatusMark kind={statusKind(node.state)} />
          </div>
        ))}
      </div>
    </section>
  );
}
