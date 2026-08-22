import { Download, FileArchive, LoaderCircle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { getArtifactContent, getArtifactDescriptor, artifactDescriptorQueryKey } from "./api";

export function ArtifactViewer({
  workspaceId,
  artifactId,
}: {
  workspaceId: string;
  artifactId: string | null;
}) {
  const descriptorQuery = useQuery({
    queryKey: artifactId ? artifactDescriptorQueryKey(workspaceId, artifactId) : ["artifact-descriptor", "none"],
    queryFn: ({ signal }) => getArtifactDescriptor(workspaceId, artifactId ?? "", signal),
    enabled: artifactId !== null,
  });
  const contentQuery = useQuery({
    queryKey: ["artifact-content", workspaceId, artifactId],
    queryFn: async ({ signal }) => {
      const blob = await getArtifactContent(workspaceId, artifactId ?? "", signal);
      return (await blob.text()).slice(0, 100_000);
    },
    enabled: artifactId !== null && descriptorQuery.data?.content_available === true,
  });

  if (artifactId === null) {
    return <section className="artifact-viewer artifact-viewer--empty" aria-label="Artifact 查看器"><span>选择 Artifact 查看内容</span></section>;
  }
  return (
    <section className="artifact-viewer" aria-labelledby="artifact-viewer-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">ARTIFACT VIEWER</span>
          <h2 id="artifact-viewer-title"><FileArchive size={16} aria-hidden="true" />{artifactId.slice(0, 12)}</h2>
        </div>
        {descriptorQuery.data?.content_available ? (
          <a className="quiet-button" href={artifactContentUrl(workspaceId, artifactId)} target="_blank" rel="noreferrer">
            <Download size={14} aria-hidden="true" />下载
          </a>
        ) : null}
      </div>
      {descriptorQuery.isPending ? <div className="trace-loading"><LoaderCircle size={16} />读取 Artifact 描述</div> : null}
      {descriptorQuery.isError ? <div className="trace-error" role="alert">Artifact 描述暂不可用。</div> : null}
      {descriptorQuery.data ? (
        <div className="artifact-meta">
          <span>{descriptorQuery.data.media_type}</span>
          <span>{formatBytes(descriptorQuery.data.byte_size)}</span>
          <span>{descriptorQuery.data.content_hash.slice(0, 12)}</span>
        </div>
      ) : null}
      {contentQuery.isPending ? <div className="trace-loading"><LoaderCircle size={16} />读取内容</div> : null}
      {contentQuery.isError ? <div className="trace-error" role="alert">Artifact 内容暂不可用。</div> : null}
      {contentQuery.data ? <pre className="artifact-content">{contentQuery.data}</pre> : null}
      {descriptorQuery.data?.content_available === false ? <span className="muted-copy">{descriptorQuery.data.unavailable_reason ?? "内容不可用"}</span> : null}
    </section>
  );
}

function artifactContentUrl(workspaceId: string, artifactId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(artifactId)}/content`;
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  return `${(value / 1_024).toFixed(1)} KB`;
}
