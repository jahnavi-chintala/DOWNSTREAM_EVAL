import type { ProductDef } from "../types";

interface Props {
  product: ProductDef;
  onShare?: () => void;
  shareLabel?: string;
}

export function Topbar({ product, onShare, shareLabel }: Props) {
  return (
    <header className="topbar">
      <div>
        <div className="crumbs">
          Evaluations &middot; {product.label}
        </div>
        <h1 className="title">{product.label} evaluation</h1>
        <div className="subtitle">{product.description}</div>
      </div>
      <div className="topbar-actions">
        {onShare && (
          <button type="button" className="btn ghost sm" onClick={onShare}>
            {shareLabel ?? "Copy share link"}
          </button>
        )}
      </div>
    </header>
  );
}
