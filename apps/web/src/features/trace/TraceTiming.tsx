import type { TraceTiming as TraceTimingValue } from "./types";

export function TraceTiming({ timing }: { timing: TraceTimingValue }) {
  return (
    <section className="trace-timing" aria-labelledby="trace-timing-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">TIMING</span>
          <h2 id="trace-timing-title">执行耗时</h2>
        </div>
        <span className="mono-label">{formatDuration(timing.total_ms)}</span>
      </div>
      <div className="timing-grid">
        <TimingMetric label="Input" value={timing.input_ms} />
        <TimingMetric label="Model" value={timing.model_ms} />
        <TimingMetric label="Tools" value={timing.tools_ms} />
        <TimingMetric label="Total" value={timing.total_ms} emphasis />
      </div>
    </section>
  );
}

function TimingMetric({ label, value, emphasis = false }: { label: string; value: number; emphasis?: boolean }) {
  return (
    <div className={`timing-metric${emphasis ? " is-emphasis" : ""}`}>
      <span>{label}</span>
      <strong>{formatDuration(value)}</strong>
    </div>
  );
}

function formatDuration(value: number): string {
  if (value < 1_000) return `${value} ms`;
  return `${(value / 1_000).toFixed(1)} s`;
}
