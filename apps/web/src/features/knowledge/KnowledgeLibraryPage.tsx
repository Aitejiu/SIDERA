import { RefreshCw, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { ApiProblem } from "@/shared/api/client";

import {
  knowledgePagesQueryKey,
  knowledgeSearchQueryKey,
  listKnowledgePages,
  searchKnowledge,
} from "./api";
import type { KnowledgePageSummary, KnowledgeSearchHit } from "./types";
import { KnowledgeSectionTabs } from "./KnowledgeSectionTabs";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const SEARCH_DEBOUNCE_MS = 300;

type LibraryRow = {
  pageId: string;
  title: string;
  summary: string;
  slug: string;
  status: string;
  tags: string[];
  sourceSummary: string;
  revisionNumber: number | null;
  updatedAt: string;
};

export function KnowledgeLibraryPage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const [searchInput, setSearchInput] = useState("");
  const [searchText, setSearchText] = useState("");
  const [status, setStatus] = useState("all");
  const [tag, setTag] = useState("");
  const [sourceType, setSourceType] = useState("all");

  useEffect(() => {
    const timer = window.setTimeout(() => setSearchText(searchInput.trim()), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  const normalizedTag = tag.trim();
  const searching = searchText.length > 0
    || status !== "all"
    || normalizedTag.length > 0
    || sourceType !== "all";
  const searchTags = normalizedTag.length > 0 ? [normalizedTag] : [];
  const sourceTypes = sourceType === "all" ? [] : [sourceType];

  const pagesQuery = useQuery({
    queryKey: knowledgePagesQueryKey(workspaceId),
    queryFn: ({ signal }) => listKnowledgePages(workspaceId, { signal }),
    enabled: workspaceId.length > 0 && !searching,
    retry: false,
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  const searchQuery = useQuery({
    queryKey: knowledgeSearchQueryKey(workspaceId, searchText, searchTags, sourceTypes, status),
    queryFn: ({ signal }) => {
      const options: Parameters<typeof searchKnowledge>[2] = {
        tags: searchTags,
        sourceTypes,
        signal,
      };
      if (status !== "all") options.status = status;
      return searchKnowledge(workspaceId, searchText, options);
    },
    enabled: workspaceId.length > 0 && searching,
    retry: false,
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });

  const rows = useMemo(
    () => searching
      ? searchHitsToRows(searchQuery.data?.items ?? [])
      : (pagesQuery.data?.items ?? []).map(pageToRow),
    [pagesQuery.data?.items, searchQuery.data?.items, searching],
  );
  const tagOptions = useMemo(
    () => uniqueTags(pagesQuery.data?.items ?? []),
    [pagesQuery.data?.items],
  );
  const activeQuery = searching ? searchQuery : pagesQuery;
  const clearFilters = () => {
    setSearchInput("");
    setSearchText("");
    setStatus("all");
    setTag("");
    setSourceType("all");
  };

  return (
    <section className="knowledge-page" aria-labelledby="knowledge-library-title">
      <div className="breadcrumb">工作区 / 知识 / 知识库</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE REFERENCE</span>
          <h1 id="knowledge-library-title">知识库</h1>
        </div>
        <div className="page-heading__actions">
          <Link
            className="quiet-button"
            to={`/workspaces/${workspaceId}/knowledge/import`}
          >
            导入 Markdown
          </Link>
          <button
            className="icon-button"
            type="button"
            onClick={() => void activeQuery.refetch()}
            aria-label="刷新知识页"
            title="刷新知识页"
          >
            <RefreshCw size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      <KnowledgeSectionTabs />
      <div className="knowledge-toolbar" aria-label="知识库筛选">
        <label className="knowledge-search-field">
          <span className="sr-only">搜索知识</span>
          <Search size={16} aria-hidden="true" />
          <input
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                setSearchText(searchInput.trim());
              }
            }}
            placeholder="搜索标题、摘要或内容"
            aria-label="搜索知识"
          />
        </label>
        <div className="knowledge-filter-fields">
          <label className="knowledge-filter-field">
            <span>状态</span>
            <select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="状态筛选">
              <option value="all">全部状态</option>
              <option value="active">生效</option>
              <option value="archived">归档</option>
            </select>
          </label>
          <label className="knowledge-filter-field">
            <span>标签</span>
            <input
              value={tag}
              onChange={(event) => setTag(event.target.value)}
              list="knowledge-tag-options"
              placeholder="全部标签"
              aria-label="标签筛选"
            />
            <datalist id="knowledge-tag-options">
              {tagOptions.map((option) => <option key={option} value={option} />)}
            </datalist>
          </label>
          <label className="knowledge-filter-field">
            <span>来源</span>
            <select
              value={sourceType}
              onChange={(event) => setSourceType(event.target.value)}
              aria-label="来源类型筛选"
            >
              <option value="all">全部来源</option>
              <option value="markdown_file">Markdown 文件</option>
              <option value="manual">手工编辑</option>
              <option value="task">任务</option>
              <option value="artifact">产物</option>
              <option value="evidence">证据</option>
              <option value="external_url">外部链接</option>
            </select>
          </label>
          <button
            className="icon-button knowledge-filter-clear"
            type="button"
            onClick={clearFilters}
            disabled={!searchInput && status === "all" && !tag && sourceType === "all"}
            aria-label="清除知识筛选"
            title="清除知识筛选"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="knowledge-results-line" aria-live="polite">
        <span className="mono-label">
          {searching ? "SEARCH RESULT" : "CANONICAL PAGES"} · {rows.length} 项
        </span>
        {searching && searchText.length === 0 ? <span className="mono-label">已应用筛选</span> : null}
      </div>

      {activeQuery.isPending ? <KnowledgeLibrarySkeleton /> : null}
      {activeQuery.isError ? (
        <KnowledgeErrorState
          message={errorMessage(activeQuery.error)}
          onRetry={() => void activeQuery.refetch()}
        />
      ) : null}
      {!activeQuery.isPending && !activeQuery.isError && rows.length === 0 ? (
        <div className="knowledge-empty" role="status">
          <span className="eyebrow">NO MATCHES</span>
          <strong>没有匹配的知识页</strong>
          <span>调整搜索词或筛选条件后再试。</span>
        </div>
      ) : null}
      {!activeQuery.isPending && !activeQuery.isError && rows.length > 0 ? (
        <KnowledgeTable rows={rows} workspaceId={workspaceId} />
      ) : null}
    </section>
  );
}

function KnowledgeTable({ rows, workspaceId }: { rows: LibraryRow[]; workspaceId: string }) {
  return (
    <div className="knowledge-table" role="table" aria-label="知识页列表">
      <div className="knowledge-table__header" role="row" aria-hidden="true">
        <span>页面</span>
        <span>状态</span>
        <span>标签</span>
        <span>来源</span>
        <span>更新</span>
      </div>
      <div className="knowledge-table__body">
        {rows.map((row) => (
          <Link
            className="knowledge-table__row"
            key={row.pageId}
            to={`/workspaces/${workspaceId}/knowledge/pages/${encodeURIComponent(row.pageId)}`}
            aria-label={`打开知识页 ${row.title}`}
          >
            <span className="knowledge-table__cell knowledge-table__cell--identity" data-label="页面">
              <strong>{row.title}</strong>
              <span>{row.summary || row.slug}</span>
              <span className="mono-label">
                {row.pageId.slice(0, 8)}{row.revisionNumber === null ? "" : ` · REV ${row.revisionNumber}`}
              </span>
            </span>
            <span className="knowledge-table__cell" data-label="状态">
              <span className={`knowledge-status knowledge-status--${row.status}`}>
                {knowledgeStatusLabel(row.status)}
              </span>
            </span>
            <span className="knowledge-table__cell knowledge-table__cell--tags" data-label="标签">
              {row.tags.length > 0 ? row.tags.map((item) => <span className="knowledge-tag" key={item}>{item}</span>) : <span className="metric-muted">无标签</span>}
            </span>
            <span className="knowledge-table__cell" data-label="来源">
              <span>{row.sourceSummary}</span>
            </span>
            <span className="knowledge-table__cell knowledge-table__cell--updated" data-label="更新">
              {formatKnowledgeDate(row.updatedAt)}
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}

function KnowledgeLibrarySkeleton() {
  return (
    <div className="knowledge-table knowledge-table--loading" aria-label="正在加载知识页">
      <div className="knowledge-table__header" aria-hidden="true"><span>页面</span><span>状态</span><span>标签</span><span>来源</span><span>更新</span></div>
      <div className="knowledge-table__body">
        {Array.from({ length: 4 }, (_, index) => (
          <div className="knowledge-table__row knowledge-table__row--skeleton" data-testid="knowledge-row-skeleton" key={index} aria-hidden="true">
            <span /><span /><span /><span /><span />
          </div>
        ))}
      </div>
    </div>
  );
}

function KnowledgeErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="async-state async-state--error knowledge-async-state" role="alert">
      <span className="async-state__mark" aria-hidden="true">!</span>
      <div>
        <strong>{message}</strong>
        <p>知识服务暂时没有返回可用的页面列表。</p>
      </div>
      <button className="quiet-button" type="button" onClick={onRetry}>重试读取知识页</button>
    </div>
  );
}

function pageToRow(page: KnowledgePageSummary): LibraryRow {
  const revision = page.current_revision;
  return {
    pageId: page.page_id,
    title: page.canonical_title,
    summary: revision?.summary ?? "",
    slug: page.slug,
    status: page.status,
    tags: revision?.tags ?? [],
    sourceSummary: revision?.change_reason || revision?.created_by || "来源未展开",
    revisionNumber: revision?.revision_number ?? null,
    updatedAt: page.updated_at,
  };
}

function searchHitsToRows(hits: KnowledgeSearchHit[]): LibraryRow[] {
  const byPage = new Map<string, LibraryRow>();
  for (const hit of hits) {
    const current = byPage.get(hit.page_id);
    if (current === undefined) {
      byPage.set(hit.page_id, {
        pageId: hit.page_id,
        title: hit.title,
        summary: hit.summary,
        slug: hit.title,
        status: "active",
        tags: [...hit.tags],
        sourceSummary: hit.source_types.length > 0 ? hit.source_types.join(" · ") : "来源未展开",
        revisionNumber: hit.revision_number,
        updatedAt: "",
      });
      continue;
    }
    current.tags = uniqueStrings([...current.tags, ...hit.tags]);
    current.sourceSummary = uniqueStrings([...current.sourceSummary.split(" · "), ...hit.source_types]).join(" · ") || "来源未展开";
  }
  return [...byPage.values()];
}

function uniqueTags(pages: KnowledgePageSummary[]): string[] {
  return uniqueStrings(pages.flatMap((page) => page.current_revision?.tags ?? [])).sort();
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.filter((value) => value.length > 0))];
}

function knowledgeStatusLabel(status: string): string {
  if (status === "active") return "生效";
  if (status === "archived") return "归档";
  return status || "未知";
}

function formatKnowledgeDate(value: string): string {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function errorMessage(error: unknown): string {
  return error instanceof ApiProblem ? "无法读取知识页" : "知识服务暂时不可用";
}
