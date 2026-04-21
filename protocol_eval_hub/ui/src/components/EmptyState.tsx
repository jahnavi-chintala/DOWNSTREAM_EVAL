import type { ProductDef } from "../types";

export function EmptyState({ product }: { product: ProductDef }) {
  return (
    <div className="empty">
      <h3>No report yet</h3>
      <p>
        Upload the required inputs above and run the evaluation. Results will appear here:
        verdict chips, metric cards, issue notes, and the full JSON / YAML / DOCX artifacts —
        all viewable and downloadable.
      </p>
      <div className="ghost-grid" aria-hidden>
        <div className="ghost-card">
          <div className="l">{product.label} · verdict</div>
          <div className="v">—</div>
        </div>
        <div className="ghost-card">
          <div className="l">Document score</div>
          <div className="v">—</div>
        </div>
        <div className="ghost-card">
          <div className="l">Passing metrics</div>
          <div className="v">—</div>
        </div>
        <div className="ghost-card">
          <div className="l">Findings</div>
          <div className="v">—</div>
        </div>
      </div>
    </div>
  );
}
