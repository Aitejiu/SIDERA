import { CheckCircle2, GitBranch, RotateCcw, ShieldCheck } from "lucide-react";

type ResultPlanSummaryProps = {
  proposalSchemaVersion: number;
  integrationSpec: unknown;
  revisionChanges?: string[];
};

type SummaryModel = {
  repositoryRef: string;
  integrationWorkerItemKey: string;
  criteria: Array<{ key: string; text: string; mode: string; checkKeys: string[] }>;
  checks: Array<{ key: string; version: number; argv: string[]; environmentRef: string }>;
  maxAutoReworkRounds: number;
  maxAutoReworkSeconds: number;
  applyOnAccept: boolean;
  targetRef: string | null;
};

export function ResultPlanSummary({
  proposalSchemaVersion,
  integrationSpec,
  revisionChanges = [],
}: ResultPlanSummaryProps) {
  const model = proposalSchemaVersion >= 5 ? readSummaryModel(integrationSpec) : null;

  return (
    <section className="result-plan-summary" aria-labelledby="result-plan-summary-title">
      <div className="result-plan-summary__heading">
        <div>
          <span className="eyebrow">RESULT INTEGRATION</span>
          <h2 id="result-plan-summary-title">成果集成出口</h2>
        </div>
        <span className="mono-label">确认前只读</span>
      </div>
      {model === null ? (
        <p className="result-plan-summary__disabled">结果集成未启用</p>
      ) : (
        <>
          <dl className="result-plan-summary__facts">
            <div>
              <dt><GitBranch size={13} aria-hidden="true" />集成出口</dt>
              <dd>{model.integrationWorkerItemKey}</dd>
              <small>{model.repositoryRef}</small>
            </div>
            <div>
              <dt><ShieldCheck size={13} aria-hidden="true" />返工额度</dt>
              <dd>最多 {model.maxAutoReworkRounds} 轮 / {model.maxAutoReworkSeconds} 秒</dd>
              <small>{model.applyOnAccept ? `接受后应用到 ${model.targetRef ?? "目标分支"}` : "默认不自动应用"}</small>
            </div>
          </dl>
          <div className="result-plan-summary__columns">
            <SummaryList icon={<CheckCircle2 size={13} aria-hidden="true" />} title="验收标准">
              {model.criteria.map((criterion) => (
                <li key={criterion.key}>
                  <strong>{criterion.text}</strong>
                  <span>{criterion.mode === "automatic" ? "自动" : "人工"} · {criterion.checkKeys.join("、") || "无需检查"}</span>
                </li>
              ))}
            </SummaryList>
            <SummaryList icon={<ShieldCheck size={13} aria-hidden="true" />} title="检查">
              {model.checks.map((check) => (
                <li key={check.key}>
                  <strong>{check.argv.join(" ")}</strong>
                  <span>{check.key} v{check.version} · {check.environmentRef}</span>
                </li>
              ))}
            </SummaryList>
          </div>
          {revisionChanges.length > 0 ? (
            <div className="result-plan-summary__revision">
              <span><RotateCcw size={13} aria-hidden="true" />Revision 变动</span>
              <ul>{revisionChanges.map((change) => <li key={change}>{change}</li>)}</ul>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

function SummaryList({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="result-plan-summary__list">
      <h3>{icon}{title}</h3>
      <ul>{children}</ul>
    </div>
  );
}

function readSummaryModel(value: unknown): SummaryModel | null {
  if (!isRecord(value)) return null;
  const criteria = readRecords(value.criteria).flatMap((item) => {
    const key = readText(item.key);
    const text = readText(item.text);
    if (key === null || text === null) return [];
    return [{
      key,
      text,
      mode: readText(item.mode) ?? "manual",
      checkKeys: readTexts(item.check_keys),
    }];
  });
  const checks = readRecords(value.checks).flatMap((item) => {
    const key = readText(item.key);
    if (key === null) return [];
    return [{
      key,
      version: readNumber(item.version, 1),
      argv: readTexts(item.argv),
      environmentRef: readText(item.environment_ref) ?? "未声明环境",
    }];
  });
  if (criteria.length === 0 || checks.length === 0) return null;
  const reworkPolicy = isRecord(value.rework_policy) ? value.rework_policy : {};
  const applyPolicy = isRecord(value.apply_policy) ? value.apply_policy : {};
  return {
    repositoryRef: readText(value.repository_ref) ?? "未声明仓库",
    integrationWorkerItemKey: readText(value.integration_worker_item_key) ?? "未声明 WorkerItem",
    criteria,
    checks,
    maxAutoReworkRounds: readNumber(reworkPolicy.max_auto_rework_rounds, 0),
    maxAutoReworkSeconds: readNumber(reworkPolicy.max_auto_rework_seconds, 0),
    applyOnAccept: applyPolicy.apply_on_accept === true,
    targetRef: readText(applyPolicy.target_ref),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function readTexts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function readText(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function readNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
