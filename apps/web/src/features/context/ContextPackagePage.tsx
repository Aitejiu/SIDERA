import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, CheckCircle2, CircleAlert, PackageOpen, RefreshCcw, ShieldCheck } from "lucide-react";
import { useEffect, useMemo } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";

import {
  contextPackageQueryKey,
  getContextPackage,
  materializeContextPackage,
} from "./api";
import { ContextManifest, knowledgeRefKey } from "./ContextManifest";
import type {
  ContextPackage,
  ContextPackageAcknowledgement,
  ContextPackageMaterialization,
} from "./types";
import { getKnowledgePage, knowledgeRevisionQueryKey } from "../knowledge/api";
import { SafeMarkdown } from "../knowledge/SafeMarkdown";
import type { KnowledgeRevision } from "../knowledge/types";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function ContextPackagePage() {
  const { workspaceId = DEFAULT_WORKSPACE_ID, packageId = "" } = useParams<{
    workspaceId: string;
    packageId: string;
  }>();
  const queryClient = useQueryClient();
  const packageQuery = useQuery({
    queryKey: contextPackageQueryKey(workspaceId, packageId),
    queryFn: ({ signal }) => getContextPackage(workspaceId, packageId, signal),
    enabled: Boolean(workspaceId && packageId),
    retry: false,
  });
  const contextPackage = packageQuery.data;
  const knowledgeRefs = useMemo(
    () => contextPackage?.knowledge_refs ?? [],
    [contextPackage],
  );
  const revisionQueries = useQueries({
    queries: knowledgeRefs.map((ref) => ({
      queryKey: knowledgeRevisionQueryKey(workspaceId, ref.page_id, ref.revision_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getKnowledgePage(
        workspaceId,
        ref.page_id,
        ref.revision_id,
        signal,
      ),
      enabled: Boolean(workspaceId && ref.page_id && ref.revision_id),
      retry: false,
    })),
  });
  const materializationKey = useMemo(
    () => materializationQueryKey(workspaceId, packageId),
    [packageId, workspaceId],
  );
  const materializationQuery = useQuery({
    queryKey: materializationKey,
    queryFn: ({ signal }) => materializeContextPackage(workspaceId, packageId, signal),
    enabled: false,
    retry: false,
    gcTime: 0,
  });

  useEffect(() => {
    return () => {
      queryClient.removeQueries({ queryKey: materializationKey, exact: true });
    };
  }, [materializationKey, queryClient]);

  const knowledgeTitles = useMemo(() => {
    const titles = new Map<string, string>();
    knowledgeRefs.forEach((ref, index) => {
      const revision = revisionQueries[index]?.data as KnowledgeRevision | undefined;
      if (revision !== undefined) titles.set(knowledgeRefKey(ref), revision.title);
    });
    return titles;
  }, [knowledgeRefs, revisionQueries]);

  return (
    <section className="context-package-page" aria-labelledby="context-package-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${encodeURIComponent(workspaceId)}/tasks`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span><span>Context Package</span>
      </div>
      <header className="context-package-page__heading">
        <div>
          <span className="eyebrow">CONTEXT PACKAGE / MANIFEST</span>
          <h1 id="context-package-title">上下文包</h1>
          <p className="context-package-page__address"><code>{packageId || "unknown"}</code></p>
        </div>
        <div className="context-package-page__heading-actions">
          <PackageOpen size={18} aria-hidden="true" />
          <span className="mono-label">MANIFEST FIRST</span>
          <button
            className="icon-button"
            type="button"
            onClick={() => {
              void packageQuery.refetch();
              if (materializationQuery.data !== undefined) {
                queryClient.removeQueries({ queryKey: materializationKey, exact: true });
              }
            }}
            aria-label="刷新上下文包"
            title="刷新上下文包"
          >
            <RefreshCcw size={16} aria-hidden="true" />
          </button>
        </div>
      </header>

      {packageQuery.isPending ? <ContextPackageState label="正在读取上下文包 manifest" /> : null}
      {packageQuery.isError ? (
        <ContextPackageState
          label="无法读取上下文包"
          detail={errorDetail(packageQuery.error)}
          actionLabel="刷新上下文包"
          onAction={() => void packageQuery.refetch()}
        />
      ) : null}
      {contextPackage ? (
        <ContextPackageContent
          contextPackage={contextPackage}
          knowledgeTitles={knowledgeTitles}
          materialization={materializationQuery.data}
          materializationError={materializationQuery.error}
          materializationLoading={materializationQuery.isFetching}
          canMaterialize={canMaterialize(contextPackage)}
          onMaterialize={() => void materializationQuery.refetch()}
        />
      ) : null}
    </section>
  );
}

function ContextPackageContent({
  contextPackage,
  knowledgeTitles,
  materialization,
  materializationError,
  materializationLoading,
  canMaterialize,
  onMaterialize,
}: {
  contextPackage: ContextPackage;
  knowledgeTitles: ReadonlyMap<string, string>;
  materialization: ContextPackageMaterialization | undefined;
  materializationError: unknown;
  materializationLoading: boolean;
  canMaterialize: boolean;
  onMaterialize: () => void;
}) {
  return (
    <>
      <div className="context-package-page__layout">
        <main className="context-package-page__main">
          <ContextPackageStatus contextPackage={contextPackage} />
          <ContextPackageFacts contextPackage={contextPackage} />
          <ContextManifest contextPackage={contextPackage} knowledgeTitles={knowledgeTitles} />
          <MaterializationPanel
            contextPackage={contextPackage}
            materialization={materialization}
            error={materializationError}
            loading={materializationLoading}
            canMaterialize={canMaterialize}
            onMaterialize={onMaterialize}
          />
        </main>
        <ContextPackageInspector contextPackage={contextPackage} materialization={materialization} />
      </div>
    </>
  );
}

function ContextPackageStatus({ contextPackage }: { contextPackage: ContextPackage }) {
  const expired = isExpired(contextPackage.expires_at);
  const revoked = contextPackage.status === "revoked";
  const blocked = !canMaterialize(contextPackage);
  return (
    <section className={`context-package-status context-package-status--${contextPackage.status}`} aria-label="上下文包状态">
      <div className="context-package-status__heading">
        <div>
          <span className="eyebrow">PACKAGE STATUS</span>
          <h2>{packageStatusLabel(contextPackage.status, expired)}</h2>
        </div>
        <span className="context-package-status__package">Package {contextPackage.status}</span>
      </div>
      <div className="context-package-status__signals">
        {revoked ? <span><CircleAlert size={14} aria-hidden="true" />已停止新用途</span> : null}
        {expired ? <span><CircleAlert size={14} aria-hidden="true" />已过期，可进行历史审计</span> : null}
        {blocked ? <span><ShieldCheck size={14} aria-hidden="true" />已禁止物化</span> : null}
        {!revoked && !expired && !blocked ? <span><CheckCircle2 size={14} aria-hidden="true" />可审计，正文仍需显式物化</span> : null}
        {contextPackage.revocation_scope ? <span>scope: {contextPackage.revocation_scope}</span> : null}
        {contextPackage.revocation_reason ? <span>原因: {contextPackage.revocation_reason}</span> : null}
      </div>
    </section>
  );
}

function ContextPackageFacts({ contextPackage }: { contextPackage: ContextPackage }) {
  const acknowledgement = latestAcknowledgement(contextPackage.acknowledgements);
  const taskPath = contextPackage.worker_item_id
    ? `WorkerItem ${shortId(contextPackage.worker_item_id)}`
    : `${contextPackage.consumer_type} ${shortId(contextPackage.consumer_id)}`;
  return (
    <section className="context-package-facts" aria-label="上下文包事实">
      <div><span>消费者</span><strong>{taskPath}</strong><small>{contextPackage.purpose} · {contextPackage.consumer_type}</small></div>
      <div><span>上下文版本</span><strong>WorkContext R{contextPackage.work_context_revision_number}</strong><small>{shortHash(contextPackage.work_context_content_hash)}</small></div>
      <div><span>知识 sections</span><strong>{contextPackage.knowledge_refs.length}</strong><small>{contextPackage.budget.actual_sections} / {contextPackage.budget.max_sections} budget</small></div>
      <div><span>有效期</span><strong>{formatTimestamp(contextPackage.expires_at)}</strong><small>{isExpired(contextPackage.expires_at) ? "已过期" : "自然过期前"}</small></div>
      <div><span>ack</span><strong>{acknowledgement ? "已确认" : "未确认"}</strong><small>{acknowledgement ? `context V${acknowledgement.context_version ?? "--"}` : "等待 consumer"}</small></div>
    </section>
  );
}

function MaterializationPanel({
  contextPackage,
  materialization,
  error,
  loading,
  canMaterialize,
  onMaterialize,
}: {
  contextPackage: ContextPackage;
  materialization: ContextPackageMaterialization | undefined;
  error: unknown;
  loading: boolean;
  canMaterialize: boolean;
  onMaterialize: () => void;
}) {
  return (
    <section className="context-materialization" aria-labelledby="context-materialization-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">OWNER AUDIT</span>
          <h2 id="context-materialization-title">物化内容</h2>
        </div>
        <button className="quiet-button" type="button" onClick={onMaterialize} disabled={!canMaterialize || loading}>
          <PackageOpen size={14} aria-hidden="true" />查看物化内容
        </button>
      </div>
      <p className="context-materialization__policy">
        Manifest 只读固定引用；正文只在本次审计中临时读取，不写回上下文包存储。
      </p>
      {!canMaterialize ? <p className="context-materialization__blocked" role="status">当前状态不允许物化正文。</p> : null}
      {loading ? <p className="context-materialization__loading" role="status">正在物化上下文正文</p> : null}
      {error ? <MaterializationError error={error} /> : null}
      {materialization ? <MaterializedContent contextPackage={contextPackage} materialization={materialization} /> : null}
    </section>
  );
}

function MaterializedContent({
  contextPackage,
  materialization,
}: {
  contextPackage: ContextPackage;
  materialization: ContextPackageMaterialization;
}) {
  return (
    <div className="context-materialization__content">
      <div className="context-materialization__summary">
        <span>schema V{materialization.schema_version}</span>
        <span>{materialization.total_byte_count} bytes</span>
        <span>{materialization.input_token_count} input tokens</span>
        <span>{materialization.project_knowledge.length} sections</span>
      </div>
      {materialization.system_constraints.objective ? (
        <div className="context-materialization__objective">
          <span className="eyebrow">OBJECTIVE</span>
          <strong>{materialization.system_constraints.objective}</strong>
        </div>
      ) : null}
      <div className="context-materialization__knowledge">
        {materialization.project_knowledge.map((section) => (
          <article className="context-materialization__knowledge-item" key={`${section.page_id}:${section.revision_id}:${section.section_key}`}>
            <div className="context-materialization__knowledge-heading">
              <div>
                <span className="eyebrow">KNOWLEDGE SECTION</span>
                <h3>{knowledgeTitle(contextPackage, section)}</h3>
              </div>
              <code>{section.section_key}</code>
            </div>
            <SafeMarkdown source={section.body_markdown} />
            <div className="context-materialization__sources">
              {section.sources.map((source) => (
                <div key={source.source_id}>
                  <strong>{source.label}</strong>
                  <span>{source.availability} · {shortHash(source.digest ?? "no-digest")}</span>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

function ContextPackageInspector({
  contextPackage,
  materialization,
}: {
  contextPackage: ContextPackage;
  materialization: ContextPackageMaterialization | undefined;
}) {
  const acknowledgement = latestAcknowledgement(contextPackage.acknowledgements);
  return (
    <aside className="context-package-inspector" aria-label="上下文包 inspector">
      <section className="context-package-inspector__section">
        <span className="eyebrow">CONTROL PLANE FACTS</span>
        <FactList facts={contextPackage.control_plane_fact_snapshot} />
      </section>
      <section className="context-package-inspector__section">
        <span className="eyebrow">ADAPTER ACK</span>
        {acknowledgement ? (
          <dl className="context-package-inspector__facts">
            <div><dt>Consumer</dt><dd>{shortId(acknowledgement.consumer_id)}</dd></div>
            <div><dt>Context</dt><dd>V{acknowledgement.context_version ?? "--"}</dd></div>
            <div><dt>Adapter config</dt><dd>V{acknowledgement.adapter_config_version}</dd></div>
            <div><dt>Module</dt><dd>{shortId(acknowledgement.adapter_module_id)}</dd></div>
          </dl>
        ) : <p className="muted-copy">还没有 consumer ack</p>}
      </section>
      <section className="context-package-inspector__section">
        <span className="eyebrow">BUDGET / USAGE</span>
        <dl className="context-package-inspector__facts">
          <div><dt>Sections</dt><dd>{contextPackage.budget.actual_sections} / {contextPackage.budget.max_sections}</dd></div>
          <div><dt>Chars</dt><dd>{contextPackage.budget.actual_chars} / {contextPackage.budget.max_chars}</dd></div>
          <div><dt>Input tokens</dt><dd>{materialization?.input_token_count ?? contextPackage.budget.actual_input_tokens} / {contextPackage.budget.max_input_tokens}</dd></div>
          <div><dt>Audit records</dt><dd>{contextPackage.materialization_audits.length}</dd></div>
        </dl>
      </section>
      {contextPackage.worker_item_id ? (
        <Link className="context-package-inspector__link" to={`/workspaces/${encodeURIComponent(contextPackage.workspace_id)}/worker-items/${encodeURIComponent(contextPackage.worker_item_id)}`}>
          返回 WorkerItem 工作区
        </Link>
      ) : null}
    </aside>
  );
}

function FactList({ facts }: { facts: Record<string, unknown> }) {
  const entries = Object.entries(facts);
  if (entries.length === 0) return <p className="muted-copy">没有附加事实</p>;
  return (
    <dl className="context-package-inspector__facts">
      {entries.slice(0, 8).map(([key, value]) => (
        <div key={key}><dt>{key}</dt><dd>{formatFact(value)}</dd></div>
      ))}
    </dl>
  );
}

function MaterializationError({ error }: { error: unknown }) {
  return (
    <div className="context-materialization__error" role="alert">
      <AlertTriangle size={15} aria-hidden="true" />
      <span>{errorDetail(error) || "物化正文失败，manifest 仍然可用。"}</span>
    </div>
  );
}

function ContextPackageState({
  label,
  detail,
  actionLabel,
  onAction,
}: {
  label: string;
  detail?: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="context-package-state" role={onAction ? "alert" : "status"}>
      <span className="eyebrow">CONTEXT PACKAGE</span>
      <strong>{label}</strong>
      {detail ? <span>{detail}</span> : null}
      {onAction && actionLabel ? <button className="quiet-button" type="button" onClick={onAction}>{actionLabel}</button> : null}
    </div>
  );
}

function materializationQueryKey(workspaceId: string, packageId: string) {
  return ["context-materialization", workspaceId, packageId] as const;
}

function canMaterialize(contextPackage: ContextPackage): boolean {
  if (contextPackage.status === "corrupt") return false;
  if (contextPackage.status === "revoked") return contextPackage.revocation_scope === "new_starts";
  return true;
}

function isExpired(expiresAt: string): boolean {
  const timestamp = Date.parse(expiresAt);
  return Number.isFinite(timestamp) && timestamp < Date.now();
}

function packageStatusLabel(status: string, expired: boolean): string {
  if (status === "revoked") return "已撤销";
  if (status === "corrupt") return "已损坏";
  if (expired) return "已过期";
  if (status === "active") return "可审计";
  return status;
}

function latestAcknowledgement(acknowledgements: readonly ContextPackageAcknowledgement[]): ContextPackageAcknowledgement | undefined {
  return [...acknowledgements].sort((left, right) => Date.parse(right.acknowledged_at) - Date.parse(left.acknowledged_at))[0];
}

function knowledgeTitle(contextPackage: ContextPackage, section: ContextPackageMaterialization["project_knowledge"][number]): string {
  const ref = contextPackage.knowledge_refs.find((item) => item.page_id === section.page_id && item.revision_id === section.revision_id && item.section_key === section.section_key);
  return ref === undefined ? `知识页 ${shortId(section.page_id)}` : `知识页 ${shortId(ref.page_id)} · ${section.section_key}`;
}

function errorDetail(error: unknown): string {
  if (error instanceof ApiProblem) return error.detail;
  return error instanceof Error ? error.message : "控制面没有返回可用数据。";
}

function formatFact(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return "不可序列化";
  }
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "未知" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function shortHash(value: string): string {
  return value.slice(0, 8);
}
