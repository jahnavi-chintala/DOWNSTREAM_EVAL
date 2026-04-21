import type { ProductKey, RecentRun } from "../types";
import { PRODUCTS, PRODUCT_ORDER } from "../products";
import { formatWhen } from "../lib/recent";
import clsx from "clsx";

interface Props {
  product: ProductKey;
  onProductChange: (p: ProductKey) => void;
  recent: RecentRun[];
  onOpenRecent: (r: RecentRun) => void;
  onClearRecent: () => void;
  busy: boolean;
}

export function Sidebar({
  product,
  onProductChange,
  recent,
  onOpenRecent,
  onClearRecent,
  busy,
}: Props) {
  return (
    <aside className="sidebar" aria-label="Navigation">
      <div className="brand">
        <span className="brand-mark">Protocol Eval Console</span>
        <span className="brand-sub">Pfizer · evaluations</span>
      </div>

      <section>
        <div className="side-label">Products</div>
        <div className="product-list">
          {PRODUCT_ORDER.map((k) => {
            const p = PRODUCTS[k];
            return (
              <button
                key={k}
                type="button"
                className={clsx("product-btn", product === k && "on")}
                onClick={() => !busy && onProductChange(k)}
                disabled={busy}
                aria-pressed={product === k}
              >
                <span className="product-dot" aria-hidden />
                <span>
                  <div className="product-label">{p.label}</div>
                  <div className="product-desc">{p.tagline}</div>
                </span>
              </button>
            );
          })}
        </div>
      </section>

      <section style={{ minHeight: 120 }}>
        <div
          className="side-label"
          style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}
        >
          <span>Recent runs</span>
          {recent.length > 0 && (
            <button
              type="button"
              onClick={onClearRecent}
              className="btn sm ghost"
              style={{ padding: "2px 6px", fontSize: 10.5, letterSpacing: 0.05 + "em" }}
            >
              clear
            </button>
          )}
        </div>
        {recent.length === 0 ? (
          <div className="recent-empty">
            No runs yet. Upload files and click <em>Run evaluation</em> to see results here.
          </div>
        ) : (
          <div className="recent-list">
            {recent.map((r) => (
              <button
                key={r.session_token}
                type="button"
                className="recent-item"
                onClick={() => onOpenRecent(r)}
                title={`${PRODUCTS[r.product].label} · ${r.study_id}`}
              >
                <div className="top">
                  <span className="sid">{r.study_id}</span>
                  <span className="when">{formatWhen(r.ran_at)}</span>
                </div>
                <div className="meta">
                  {PRODUCTS[r.product].label}
                  {r.verdict ? ` · ${r.verdict}` : ""}
                  {r.headline_score != null ? ` · ${r.headline_score}` : ""}
                </div>
              </button>
            ))}
          </div>
        )}
      </section>

      <div className="sidebar-footer">
        <span>Status: {busy ? "running evaluation…" : "idle"}</span>
        <span>
          <a href="/docs" target="_blank" rel="noreferrer">
            OpenAPI
          </a>{" "}
          ·{" "}
          <a href="/health" target="_blank" rel="noreferrer">
            Health
          </a>
        </span>
      </div>
    </aside>
  );
}
