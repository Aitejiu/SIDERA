import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ReactNode } from "react";

export function SafeMarkdown({ source }: { source: string }) {
  return (
    <div className="safe-markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={safeComponents}
      >
        {source}
      </Markdown>
    </div>
  );
}

const safeComponents: Components = {
  a: ({ href, children }) => (
    <SafeLink href={href}>{children}</SafeLink>
  ),
};

function SafeLink({ href, children }: { href?: string | undefined; children?: ReactNode }) {
  if (!isAllowedLink(href)) {
    return <span className="safe-markdown__blocked-link">{children}</span>;
  }

  return (
    <a
      href={href}
      target={href?.startsWith("mailto:") ? undefined : "_blank"}
      rel="noopener noreferrer"
    >
      {children}
    </a>
  );
}

function isAllowedLink(value: string | undefined): value is string {
  if (value === undefined || value.trim() === "") {
    return false;
  }
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" || url.protocol === "mailto:";
  } catch {
    return false;
  }
}
