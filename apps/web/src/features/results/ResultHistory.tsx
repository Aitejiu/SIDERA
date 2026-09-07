import type { TaskResultOverview } from "./api";

export function ResultHistory({
  view,
  selectedCandidateId,
  onSelect,
}: {
  view: TaskResultOverview;
  selectedCandidateId: string;
  onSelect: (candidateId: string) => void;
}) {
  return (
    <section className="result-history" aria-labelledby="result-history-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">CANDIDATE HISTORY</span>
          <h2 id="result-history-title">成果版本</h2>
        </div>
        <span className="mono-label">{view.candidate_history.items.length} 个版本</span>
      </div>
      {view.candidate_history.items.length === 0 ? (
        <p className="muted-copy">还没有成果版本。</p>
      ) : (
        <div className="result-history__list">
          {view.candidate_history.items.map((candidate) => (
            <button
              className={candidate.candidate_id === selectedCandidateId ? "is-active" : ""}
              type="button"
              key={candidate.candidate_id}
              onClick={() => onSelect(candidate.candidate_id)}
            >
              <strong>{candidate.is_current ? "当前成果" : "历史成果"}</strong>
              <span>{shortHash(candidate.candidate_hash)}</span>
              <small>{candidate.result_commit ? `commit ${shortHash(candidate.result_commit)}` : "没有结果 commit"}</small>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function shortHash(value: string): string {
  return value.slice(0, 12);
}
