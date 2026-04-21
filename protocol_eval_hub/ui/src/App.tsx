import { useCallback, useEffect, useMemo, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { UploadPanel } from "./components/UploadPanel";
import { EmptyState } from "./components/EmptyState";
import { ResultsView } from "./components/ResultsView";
import { PRODUCTS } from "./products";
import type { EvalResponse, ProductKey, RecentRun } from "./types";
import { runEval } from "./api";
import { addRecent, clearRecent, loadRecent } from "./lib/recent";

function initialProduct(): ProductKey {
  try {
    const params = new URLSearchParams(window.location.search);
    const p = params.get("product") as ProductKey | null;
    if (p && (PRODUCTS as Record<string, unknown>)[p]) return p;
    const legacyMatch = window.location.pathname.match(/^\/(risk|pipd|cmp|dmp)\b/);
    if (legacyMatch) return legacyMatch[1] as ProductKey;
  } catch {
    // ignore
  }
  return "risk";
}

export default function App() {
  const [product, setProduct] = useState<ProductKey>(initialProduct);
  const [files, setFiles] = useState<Record<string, File | null>>({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EvalResponse | null>(null);
  const [recent, setRecent] = useState<RecentRun[]>(() => loadRecent());
  const [shareLabel, setShareLabel] = useState<string | null>(null);

  const productDef = PRODUCTS[product];

  useEffect(() => {
    setFiles({});
    setError(null);
    setResult(null);
    setShareLabel(null);
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("product", product);
      window.history.replaceState({}, "", url.toString());
    } catch {
      // ignore
    }
  }, [product]);

  const canRun = useMemo(
    () => productDef.fields.filter((f) => f.required).every((f) => !!files[f.name]),
    [productDef, files]
  );

  const onFileChange = useCallback((name: string, file: File | null) => {
    setFiles((prev) => ({ ...prev, [name]: file }));
  }, []);

  const onRun = useCallback(async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await runEval(productDef, files);
      setResult(res);
      const updated = addRecent(res);
      setRecent(updated);
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setRunning(false);
    }
  }, [productDef, files]);

  const onShare = useCallback(() => {
    if (!result) return;
    const url = new URL(window.location.href);
    url.searchParams.set("product", product);
    url.searchParams.set("session", result.session_token);
    navigator.clipboard
      .writeText(url.toString())
      .then(() => {
        setShareLabel("Copied ✓");
        setTimeout(() => setShareLabel(null), 1500);
      })
      .catch(() => {
        setShareLabel("Copy failed");
        setTimeout(() => setShareLabel(null), 1500);
      });
  }, [product, result]);

  const onOpenRecent = useCallback((r: RecentRun) => {
    setProduct(r.product);
    setResult({
      product: r.product,
      study_id: r.study_id,
      verdict: r.verdict,
      session_token: r.session_token,
      downloads: r.downloads,
      ran_at: r.ran_at,
      preview: null,
    });
    setError(null);
  }, []);

  const onClearRecent = useCallback(() => {
    clearRecent();
    setRecent([]);
  }, []);

  return (
    <div className="app-shell">
      <Sidebar
        product={product}
        onProductChange={setProduct}
        recent={recent}
        onOpenRecent={onOpenRecent}
        onClearRecent={onClearRecent}
        busy={running}
      />
      <main className="main">
        <div className="main-inner">
          <Topbar
            product={productDef}
            onShare={result ? onShare : undefined}
            shareLabel={shareLabel ?? undefined}
          />

          <UploadPanel
            product={productDef}
            files={files}
            onFileChange={onFileChange}
            onRun={onRun}
            running={running}
            canRun={canRun}
            error={error}
          />

          {result ? (
            <ResultsView product={productDef} result={result} />
          ) : (
            <EmptyState product={productDef} />
          )}
        </div>
      </main>
    </div>
  );
}
