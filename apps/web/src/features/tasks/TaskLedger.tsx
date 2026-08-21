import { Search } from "lucide-react";
import { useMemo, useState } from "react";

import { StatusMark } from "@/shared/ui/StatusMark";

import { statusKind } from "./TaskNode";
import type { TaskNode } from "./types";

export function TaskLedger({
  nodes,
  selectedWorkItemKey,
  onSelect,
}: {
  nodes: readonly TaskNode[];
  selectedWorkItemKey: string | null;
  onSelect: (workItemKey: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const filteredNodes = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return nodes.filter((node) => {
      const matchesSearch =
        needle.length === 0 ||
        node.title.toLocaleLowerCase().includes(needle) ||
        node.work_item_key.toLocaleLowerCase().includes(needle) ||
        (node.worker_display_name ?? "").toLocaleLowerCase().includes(needle);
      return matchesSearch && (stateFilter === "all" || node.state === stateFilter);
    });
  }, [nodes, search, stateFilter]);

  return (
    <section className="task-ledger" aria-labelledby="ledger-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">TASK LEDGER</span>
          <h2 id="ledger-title">任务表</h2>
        </div>
        <span className="mono-label">{filteredNodes.length} / {nodes.length}</span>
      </div>
      <div className="ledger-controls">
        <label className="search-field">
          <Search size={15} aria-hidden="true" />
          <span className="sr-only">搜索任务</span>
          <input
            aria-label="搜索任务"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索 key / 标题 / Worker"
          />
        </label>
        <label className="filter-field">
          <span className="sr-only">状态筛选</span>
          <select aria-label="状态筛选" value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}>
            <option value="all">全部状态</option>
            <option value="running">运行中</option>
            <option value="planned">计划中</option>
            <option value="succeeded">已完成</option>
            <option value="failed">失败</option>
          </select>
        </label>
      </div>
      <div className="ledger-list">
        {filteredNodes.map((node) => (
          <button
            key={node.work_item_key}
            type="button"
            className={`ledger-row${node.work_item_key === selectedWorkItemKey ? " is-selected" : ""}`}
            data-selected={node.work_item_key === selectedWorkItemKey ? "true" : "false"}
            onClick={() => onSelect(node.work_item_key)}
            aria-label={`在任务表中选择${node.title}`}
          >
            <span className="ledger-row__title">
              <strong>{node.title}</strong>
              <span className="mono-label">{node.work_item_key}</span>
            </span>
            <StatusMark kind={statusKind(node.state)} />
            <span className="ledger-row__action">{node.current_action ?? (node.state === "planned" ? "等待物化" : "暂无动作")}</span>
            <span className="ledger-row__worker">{node.worker_display_name ?? (node.required_capabilities.join(" · ") || "未分配")}</span>
            <span className="ledger-row__evidence">{node.evidence_satisfied}/{node.evidence_required} Evidence</span>
          </button>
        ))}
      </div>
    </section>
  );
}
