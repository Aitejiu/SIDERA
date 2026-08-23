import { useParams } from "react-router-dom";

export function PlanningPage() {
  const { planningRequestId } = useParams<{ planningRequestId: string }>();
  const shortRequestId = planningRequestId?.slice(0, 8) ?? "未知";

  return (
    <section className="planning-page" aria-labelledby="planning-request-title">
      <div className="breadcrumb">工作区 / 任务 / 规划请求</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">PLANNING REQUEST</span>
          <h1 id="planning-request-title">规划请求</h1>
        </div>
        <span className="mono-label">请求 {shortRequestId}</span>
      </div>
      <div className="planning-page__empty">
        <span className="eyebrow">REQUEST CREATED</span>
        <strong>规划请求已创建</strong>
        <p>控制面正在准备规划结果，页面会保留这次请求的审计入口。</p>
      </div>
    </section>
  );
}
