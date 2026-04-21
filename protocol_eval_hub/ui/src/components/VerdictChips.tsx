import type { EvalPreview } from "../types";
import { verdictTone } from "../lib/format";

interface Props {
  verdict: string | null | undefined;
  preview: EvalPreview | null | undefined;
}

export function VerdictChips({ verdict, preview }: Props) {
  const h = preview?.headline || {};
  const tone = verdictTone(verdict);
  const rag = h.rag_traffic_light as string | undefined;
  const ragTone = verdictTone(rag);
  const score = h.document_score;
  const ta = (h.ta || h.therapeutic_area) as string | undefined;
  const phase = h.phase as string | undefined;

  const passCount = (preview?.metric_rows || []).filter((r) => r.pass === true).length;
  const failCount = (preview?.metric_rows || []).filter((r) => r.pass === false).length;

  return (
    <div className="chip-row">
      {verdict && (
        <span className={`chip ${tone}`}>
          <span className="dot" />
          Verdict: {String(verdict)}
        </span>
      )}
      {rag && (
        <span className={`chip ${ragTone}`}>
          <span className="dot" />
          Signal: {String(rag)}
        </span>
      )}
      {score != null && score !== "" && (
        <span className="chip">
          <span className="dot" />
          Doc score: {String(score)}
        </span>
      )}
      {preview?.metric_rows && preview.metric_rows.length > 0 && (
        <>
          <span className="chip pass" title="Metrics passing">
            <span className="dot" />
            {passCount} passing
          </span>
          {failCount > 0 && (
            <span className="chip fail" title="Metrics failing">
              <span className="dot" />
              {failCount} failing
            </span>
          )}
        </>
      )}
      {ta && (
        <span className="chip quiet">
          <span className="dot" />
          TA · {ta}
        </span>
      )}
      {phase && (
        <span className="chip quiet">
          <span className="dot" />
          Phase · {phase}
        </span>
      )}
    </div>
  );
}
