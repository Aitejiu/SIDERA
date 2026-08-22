import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { ArtifactViewer } from "./ArtifactViewer";

export function ArtifactPage() {
  const { workspaceId = "", artifactId = "" } = useParams<{ workspaceId: string; artifactId: string }>();

  return (
    <section className="artifact-page" aria-labelledby="artifact-page-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span><span>Artifact</span>
      </div>
      <header className="page-heading">
        <div>
          <span className="eyebrow">ARTIFACT · HASH ADDRESSED</span>
          <h1 id="artifact-page-title">Artifact 查看器</h1>
          <p className="trace-address-line"><code>{artifactId}</code></p>
        </div>
      </header>
      <ArtifactViewer workspaceId={workspaceId} artifactId={artifactId || null} />
    </section>
  );
}
