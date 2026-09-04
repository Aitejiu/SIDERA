import { useQuery } from "@tanstack/react-query";
import { Activity, AlertCircle, CheckCircle2, Mail, RefreshCcw } from "lucide-react";
import { useParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";

import {
  getNotificationReadiness,
  getNotificationSummary,
  listNotificationModules,
  notificationModulesQueryKey,
  notificationReadinessQueryKey,
  notificationSummaryQueryKey,
} from "./api";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function NotificationSettingsPage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const summaryQuery = useQuery({
    queryKey: notificationSummaryQueryKey(workspaceId),
    queryFn: ({ signal }) => getNotificationSummary(workspaceId, signal),
  });
  const readinessQuery = useQuery({
    queryKey: notificationReadinessQueryKey(workspaceId),
    queryFn: ({ signal }) => getNotificationReadiness(workspaceId, signal),
  });
  const modulesQuery = useQuery({
    queryKey: notificationModulesQueryKey(workspaceId),
    queryFn: ({ signal }) => listNotificationModules(workspaceId, signal),
  });

  return (
    <section className="workspace-page notification-settings-page" aria-labelledby="notification-settings-title">
      <div className="breadcrumb">工作区 / 设置</div>
      <header className="notification-settings-page__heading">
        <div className="workspace-page__title">
          <span className="workspace-page__icon"><Mail size={18} aria-hidden="true" /></span>
          <div>
            <span className="eyebrow">NOTIFICATION OPERATIONS</span>
            <h1 id="notification-settings-title">通知设置</h1>
          </div>
        </div>
        <span className="mono-label">CHANNEL LIFECYCLE</span>
      </header>

      <div className="notification-settings-page__metrics" aria-label="通知运行概览">
        <Metric label="需要处理" value={summaryQuery.data?.open_count} />
        <Metric label="进度通报" value={summaryQuery.data?.progress_count} />
        <Metric label="渠道异常" value={summaryQuery.data?.channel_issue_count} />
      </div>

      <div className="notification-settings-page__columns">
        <SettingsSection title="渠道模块" icon={<Activity size={16} aria-hidden="true" />}>
          <AsyncState
            isLoading={modulesQuery.isPending}
            isError={modulesQuery.isError}
            isEmpty={modulesQuery.data?.length === 0}
            errorMessage="无法读取渠道模块"
            retryLabel="重试读取渠道模块"
            onRetry={() => void modulesQuery.refetch()}
          >
            <div className="notification-module-list">
              {modulesQuery.data?.map((module) => (
                <div className="notification-module-row" key={module.id}>
                  <div>
                    <strong>{module.instance_name}</strong>
                    <span>{module.provider} · 配置 V{module.config_version}</span>
                  </div>
                  <span className={`lifecycle-status lifecycle-status--${module.instance_lifecycle}`}>
                    {module.instance_lifecycle}
                  </span>
                </div>
              ))}
            </div>
          </AsyncState>
        </SettingsSection>

        <SettingsSection title="运行就绪度" icon={<CheckCircle2 size={16} aria-hidden="true" />}>
          <AsyncState
            isLoading={readinessQuery.isPending}
            isError={readinessQuery.isError}
            errorMessage="无法读取通知就绪度"
            retryLabel="重试读取就绪度"
            onRetry={() => void readinessQuery.refetch()}
          >
            {readinessQuery.data ? (
              <div className="notification-readiness">
                <div className="notification-readiness__status">
                  {readinessQuery.data.status === "ready" ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
                  <strong>{readinessQuery.data.status === "ready" ? "已就绪" : "需要处理"}</strong>
                  <span>{formatTime(readinessQuery.data.as_of)}</span>
                </div>
                <ul>
                  {readinessQuery.data.checks.map((check) => (
                    <li key={check.name}>
                      <span>{check.name}</span>
                      <span>{check.ready ? "通过" : check.summary}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </AsyncState>
        </SettingsSection>
      </div>

      <div className="notification-settings-page__diagnostics">
        <RefreshCcw size={16} aria-hidden="true" />
        <div>
          <strong>积压与耗尽投递</strong>
          <span>投递失败不会暂停任务。打开信箱中的对应线程可查看诊断并在确认原因后重新投递。</span>
        </div>
      </div>
    </section>
  );
}

function SettingsSection({
  title,
  icon,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="notification-settings-section">
      <h2>{icon}{title}</h2>
      {children}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: number | undefined }) {
  return <span>{label} {value ?? "—"}</span>;
}

function formatTime(timestamp: string): string {
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
