import type { PlanningRequest } from "./types";

export function PlanningInspector({ request }: { request: PlanningRequest }) {
  return (
    <aside className="planning-inspector" aria-label="规划请求信息">
      <section className="planning-inspector__section">
        <span className="eyebrow">PLANNING PROFILE</span>
        <strong>{request.profile?.display_name ?? request.profile_key ?? "未记录配置"}</strong>
        <span>{request.profile?.planner_provider ?? "未知 provider"} · v{request.planning_profile_version}</span>
      </section>
      <section className="planning-inspector__section">
        <span className="eyebrow">REQUEST</span>
        <dl>
          <div><dt>Brief</dt><dd>R{request.current_brief_revision}</dd></div>
          <div><dt>版本</dt><dd>V{request.version}</dd></div>
          <div><dt>更新时间</dt><dd>{formatTimestamp(request.updated_at)}</dd></div>
        </dl>
      </section>
      <section className="planning-inspector__section">
        <span className="eyebrow">USAGE</span>
        <dl>
          <div><dt>输入 tokens</dt><dd>{request.usage.input_tokens}</dd></div>
          <div><dt>输出 tokens</dt><dd>{request.usage.output_tokens}</dd></div>
          <div><dt>运行耗时</dt><dd>{request.usage.runtime_seconds}s</dd></div>
          <div><dt>成本</dt><dd>{request.usage.cost_microunits} μu</dd></div>
        </dl>
      </section>
    </aside>
  );
}

function formatTimestamp(timestamp: string): string {
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
