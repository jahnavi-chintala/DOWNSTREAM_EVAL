import type { EvalResponse, ProductDef } from "../types";
import { VerdictChips } from "./VerdictChips";
import { MetricCards } from "./MetricCards";
import { NotesList } from "./NotesList";
import { DownloadBar } from "./DownloadBar";
import { ArtifactTabs } from "./ArtifactTabs";
import { TracingPanel } from "./TracingPanel";
import { PipdNearMissPanel } from "./PipdNearMissPanel";
import { summariseCounts } from "../lib/format";

interface Props {
  product: ProductDef;
  result: EvalResponse;
}

export function ResultsView({ product, result }: Props) {
  const preview = result.preview;
  const rows = preview?.metric_rows || [];
  const notes = preview?.failure_notes || [];
  const counts = summariseCounts(preview?.counts as Record<string, unknown> | undefined);
  const scenario = preview?.scenario ? `Scenario ${preview.scenario}` : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <section className="panel">
        <div className="panel-body">
          <div className="result-head">
            <div className="result-title">
              <span className="eyebrow">
                {product.label}
                {scenario ? ` · ${scenario}` : ""}
              </span>
              <h2>Study {result.study_id}</h2>
              <div className="result-meta">
                {result.verdict && (
                  <span>
                    <strong>Verdict:</strong> {result.verdict}
                  </span>
                )}
                {counts && <span>{counts}</span>}
                <span>
                  <strong>Session:</strong> {result.session_token.slice(0, 10)}…
                </span>
              </div>
            </div>
            <VerdictChips verdict={result.verdict} preview={preview} />
          </div>

          <div style={{ marginTop: 18 }}>
            <DownloadBar downloads={result.downloads} docHint={preview?.doc_hint} />
          </div>
        </div>
      </section>

      {rows.length > 0 && (
        <section className="panel">
          <div className="panel-header">
            <h3>Metrics & signals</h3>
            <span className="hint">
              {rows.filter((r) => r.pass === true).length} of {rows.length} passing
            </span>
          </div>
          <div className="panel-body">
            <MetricCards rows={rows} />
          </div>
        </section>
      )}

      {notes.length > 0 && (
        <section className="panel">
          <div className="panel-header">
            <h3>Findings & follow-ups</h3>
            <span className="hint">{notes.length} items</span>
          </div>
          <div className="panel-body">
            <NotesList notes={notes} tone="warn" />
          </div>
        </section>
      )}

      {product.key === "pipd" &&
        preview?.near_misses_by_category &&
        preview.near_misses_by_category.length > 0 && (
          <PipdNearMissPanel blocks={preview.near_misses_by_category} />
        )}

      {preview?.tracing && <TracingPanel tracing={preview.tracing} />}

      <section className="panel" style={{ padding: 0, border: "none", background: "transparent" }}>
        <ArtifactTabs
          downloads={result.downloads}
          productPrefix={product.prefix}
          sessionToken={result.session_token}
        />
      </section>
    </div>
  );
}
