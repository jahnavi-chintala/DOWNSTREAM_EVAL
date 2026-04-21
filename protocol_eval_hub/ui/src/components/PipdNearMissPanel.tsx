import type { PipdNearMissCategoryBlock } from "../types";

interface Props {
  blocks: PipdNearMissCategoryBlock[];
}

function esc(s: string | null | undefined) {
  if (s == null || s === "") return "—";
  return String(s);
}

export function PipdNearMissPanel({ blocks }: Props) {
  if (!blocks.length) return null;

  const totalAlgo = blocks.reduce((n, b) => n + b.algorithmic_near_misses.length, 0);
  const totalSem = blocks.reduce((n, b) => n + b.semantic_review_pairs.length, 0);

  return (
    <section className="panel">
      <div className="panel-header">
        <h3>M1 near-misses by category</h3>
        <span className="hint">
          {totalAlgo} algorithmic · {totalSem} semantic review (all {blocks.length} categories)
        </span>
      </div>
      <div className="panel-body">
        <p className="hint" style={{ marginTop: 0, marginBottom: 14 }}>
          Ground truth (GT) subcategory text vs generated line when the evaluator awards partial credit
          (numbering error, truncation, paraphrase, or LLM-confirmed semantic equivalent). Empty
          categories show no pairs.
        </p>
        <div className="nm-cat-list">
          {blocks.map((b) => (
            <details key={b.category_num} className="nm-cat">
              <summary className="nm-cat-summary">
                <span className="nm-cat-title">
                  Cat {b.category_num}. {b.category_name}
                </span>
                <span className="nm-cat-badges">
                  {b.algorithmic_near_misses.length > 0 && (
                    <span className="eyebrow" style={{ color: "var(--warn)" }}>
                      {b.algorithmic_near_misses.length} near-miss
                    </span>
                  )}
                  {b.semantic_review_pairs.length > 0 && (
                    <span className="eyebrow" style={{ color: "var(--accent)" }}>
                      {b.semantic_review_pairs.length} semantic
                    </span>
                  )}
                  {b.total_pairs === 0 && (
                    <span className="eyebrow" style={{ color: "var(--text-subtle)" }}>
                      none
                    </span>
                  )}
                </span>
              </summary>
              {b.total_pairs === 0 ? (
                <p className="hint nm-empty">No near-miss or semantic pairs in this category.</p>
              ) : (
                <div className="nm-tables">
                  {b.algorithmic_near_misses.length > 0 && (
                    <div className="nm-block">
                      <h4 className="nm-sub">Algorithmic near-miss</h4>
                      <table className="nm-table">
                        <thead>
                          <tr>
                            <th>GT (CSV)</th>
                            <th>Generated</th>
                            <th>Kind</th>
                            <th>Credit</th>
                          </tr>
                        </thead>
                        <tbody>
                          {b.algorithmic_near_misses.map((row, i) => (
                            <tr key={`a-${i}`}>
                              <td>{esc(row.gt_text)}</td>
                              <td>{esc(row.generated_text)}</td>
                              <td>
                                {esc(row.root_cause)}
                                {row.tier ? ` (tier ${row.tier})` : ""}
                              </td>
                              <td>{row.credit != null ? String(row.credit) : "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {b.semantic_review_pairs.length > 0 && (
                    <div className="nm-block">
                      <h4 className="nm-sub">Semantic review (LLM)</h4>
                      <table className="nm-table">
                        <thead>
                          <tr>
                            <th>GT (CSV)</th>
                            <th>Generated</th>
                            <th>Verdict / credit</th>
                            <th>Note</th>
                          </tr>
                        </thead>
                        <tbody>
                          {b.semantic_review_pairs.map((row, i) => (
                            <tr key={`s-${i}`}>
                              <td>{esc(row.gt_text)}</td>
                              <td>{esc(row.generated_text)}</td>
                              <td>
                                {esc(row.verdict)}
                                {row.credit != null ? ` · ${row.credit}` : ""}
                              </td>
                              <td className="nm-note">{row.reason || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </details>
          ))}
        </div>
      </div>
    </section>
  );
}
