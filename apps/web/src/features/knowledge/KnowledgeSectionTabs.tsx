import { BookOpen, FilePlus2, Inbox } from "lucide-react";
import { Link, useLocation, useParams } from "react-router-dom";

export function KnowledgeSectionTabs({ pendingCount }: { pendingCount?: number }) {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? "";
  const location = useLocation();
  const base = `/workspaces/${workspaceId}/knowledge`;
  const current = location.pathname.endsWith("/inbox")
    ? "inbox"
    : location.pathname.endsWith("/import")
      ? "import"
      : "library";

  return (
    <nav className="knowledge-section-tabs" aria-label="知识视图">
      <Link
        className={`knowledge-section-tab${current === "library" ? " is-active" : ""}`}
        to={base}
        aria-current={current === "library" ? "page" : undefined}
      >
        <BookOpen size={15} aria-hidden="true" />
        知识库
      </Link>
      <Link
        className={`knowledge-section-tab${current === "inbox" ? " is-active" : ""}`}
        to={`${base}/inbox`}
        aria-current={current === "inbox" ? "page" : undefined}
      >
        <Inbox size={15} aria-hidden="true" />
        收件箱
        {pendingCount !== undefined ? (
          <span className="knowledge-count-badge" aria-label={`待审核 ${pendingCount}`}>
            {boundedCount(pendingCount)}
          </span>
        ) : null}
      </Link>
      <Link
        className={`knowledge-section-tab${current === "import" ? " is-active" : ""}`}
        to={`${base}/import`}
        aria-current={current === "import" ? "page" : undefined}
      >
        <FilePlus2 size={15} aria-hidden="true" />
        导入 Markdown
      </Link>
    </nav>
  );
}

export function boundedCount(value: number): string {
  return value > 99 ? "99+" : String(value);
}
