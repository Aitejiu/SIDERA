import { FileText, RefreshCw } from "lucide-react";
import { useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { buildSubmitKnowledgeCandidateCommand, submitKnowledgeCandidate } from "./api";
import { ApiProblem, createCommandIdentity } from "@/shared/api/client";
import type { SubmitCandidatePayload } from "./types";
import { KnowledgeSectionTabs } from "./KnowledgeSectionTabs";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
export const MAX_MARKDOWN_BYTES = 1024 * 1024;

type ImportedDocument = {
  fileName: string;
  byteLength: number;
  digest: string;
  content: string;
};

export function KnowledgeImportPage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [document, setDocument] = useState<ImportedDocument | null>(null);
  const [title, setTitle] = useState("");
  const [slug, setSlug] = useState("");
  const [tags, setTags] = useState("");
  const [sourcePath, setSourcePath] = useState("");
  const [reading, setReading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file === undefined) return;
    setDocument(null);
    setSubmitError(null);
    setFileError(null);
    if (file.size > MAX_MARKDOWN_BYTES) {
      setFileError("Markdown 文件不能超过 1 MiB");
      return;
    }
    setReading(true);
    try {
      const bytes = await file.arrayBuffer();
      const content = decodeUtf8(bytes);
      const nextTitle = extractTitle(content, file.name);
      const digest = await sha256Hex(bytes);
      setDocument({ fileName: file.name, byteLength: file.size, digest, content });
      setTitle(nextTitle);
      setSlug(slugify(nextTitle));
      setSourcePath(file.name);
    } catch (error) {
      setFileError(error instanceof Error ? error.message : "无法读取 Markdown 文件");
    } finally {
      setReading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (document === null || reading) return;
    const normalizedTitle = title.trim();
    const normalizedSlug = slug.trim();
    if (normalizedTitle === "" || normalizedSlug === "") {
      setSubmitError("标题和 slug 不能为空");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    const payload: SubmitCandidatePayload = {
      candidate_schema_version: 1,
      candidate_id: createCommandIdentity().commandId,
      operation: "create_page",
      target_page_id: null,
      expected_revision_id: null,
      expected_revision_hash: null,
      title: normalizedTitle,
      slug: normalizedSlug,
      summary: "",
      tags: parseTags(tags),
      sections: [{
        section_key: "document",
        heading: normalizedTitle,
        summary: "",
        body_markdown: document.content,
        ordinal: 0,
      }],
      relations: [],
      sources: [{
        source_id: null,
        source_type: "markdown_file",
        label: document.fileName,
        uri: null,
        workspace_relative_path: sourcePath.trim() || document.fileName,
        git_revision: null,
        digest: document.digest,
        metadata: { byte_length: document.byteLength },
      }],
    };
    try {
      await submitKnowledgeCandidate(buildSubmitKnowledgeCandidateCommand({ workspaceId, payload }));
      navigate(`/workspaces/${workspaceId}/knowledge/candidates/${encodeURIComponent(payload.candidate_id)}`);
    } catch (error) {
      setSubmitError(error instanceof ApiProblem ? error.detail : "候选提交失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  const canSubmit = document !== null && !reading && !submitting && fileError === null;
  return (
    <section className="knowledge-page knowledge-import-page" aria-labelledby="knowledge-import-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${workspaceId}/knowledge`}>工作区 / 知识</Link> / 导入 Markdown
      </div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE INGESTION</span>
          <h1 id="knowledge-import-title">导入 Markdown</h1>
        </div>
        <span className="mono-label">CANDIDATE ONLY · REVIEW REQUIRED</span>
      </div>
      <KnowledgeSectionTabs />

      <form className="knowledge-import-layout" onSubmit={handleSubmit} noValidate>
        <div className="knowledge-import-main">
          <section className="knowledge-form-section" aria-labelledby="knowledge-file-title">
            <div className="knowledge-form-section__heading">
              <div><span className="eyebrow">SOURCE FILE</span><h2 id="knowledge-file-title">Markdown 文件</h2></div>
              <FileText size={18} aria-hidden="true" />
            </div>
            <label className="knowledge-file-picker">
              <span>选择文件</span>
              <input ref={inputRef} type="file" accept=".md,.markdown,text/markdown" onChange={(event) => void handleFileChange(event)} aria-label="Markdown 文件" />
            </label>
            {fileError ? <p className="knowledge-form-error" role="alert">{fileError}</p> : null}
            {reading ? <p className="knowledge-form-note" role="status"><RefreshCw size={14} aria-hidden="true" />正在读取文件</p> : null}
            {document ? <FileMetadata document={document} /> : null}
          </section>

          <section className="knowledge-form-section" aria-labelledby="knowledge-metadata-title">
            <div className="knowledge-form-section__heading">
              <div><span className="eyebrow">CANDIDATE METADATA</span><h2 id="knowledge-metadata-title">候选信息</h2></div>
            </div>
            <div className="knowledge-form-grid">
              <label className="knowledge-field"><span>标题</span><input value={title} onChange={(event) => setTitle(event.target.value)} aria-label="标题" /></label>
              <label className="knowledge-field"><span>slug</span><input value={slug} onChange={(event) => setSlug(event.target.value)} aria-label="slug" /></label>
              <label className="knowledge-field"><span>标签</span><input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="用逗号分隔" aria-label="标签" /></label>
              <label className="knowledge-field"><span>来源路径</span><input value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} aria-label="来源路径" /></label>
            </div>
          </section>
          {submitError ? <p className="knowledge-form-error" role="alert">{submitError}</p> : null}
          <div className="knowledge-import-actions">
            <button className="quiet-button" type="submit" disabled={!canSubmit}>提交待审核候选</button>
            <span className="mono-label">提交后进入知识收件箱</span>
          </div>
        </div>

        <aside className="knowledge-preview" aria-labelledby="knowledge-preview-title">
          <div className="knowledge-form-section__heading">
            <div><span className="eyebrow">PLAINTEXT PREVIEW</span><h2 id="knowledge-preview-title">内容预览</h2></div>
            <span className="mono-label">SAFE TEXT</span>
          </div>
          <pre className="knowledge-preview__body">{document?.content ?? "选择 Markdown 文件后显示内容"}</pre>
        </aside>
      </form>
    </section>
  );
}

function FileMetadata({ document }: { document: ImportedDocument }) {
  return (
    <dl className="knowledge-file-metadata">
      <div><dt>文件</dt><dd>{document.fileName}</dd></div>
      <div><dt>字节数</dt><dd>{document.byteLength.toLocaleString("zh-CN")}</dd></div>
      <div><dt>SHA-256</dt><dd><code>{document.digest}</code></dd></div>
    </dl>
  );
}

function decodeUtf8(bytes: ArrayBuffer): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("不是有效的 UTF-8 Markdown 文件");
  }
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  if (globalThis.crypto?.subtle === undefined) {
    throw new Error("当前环境无法计算文件摘要");
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function extractTitle(content: string, fileName: string): string {
  const heading = content.match(/^\s*#\s+(.+?)\s*$/m)?.[1]?.trim();
  if (heading) return heading;
  return fileName.replace(/\.(markdown|md)$/i, "").replace(/[-_]+/g, " ").trim() || "Imported document";
}

function slugify(value: string): string {
  const slug = value.toLowerCase().trim().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-+|-+$/g, "");
  return slug || "imported-document";
}

function parseTags(value: string): string[] {
  return [...new Set(value.split(",").map((tag) => tag.trim()).filter(Boolean))];
}
