import { useEffect, useRef, useState } from "react";
import { fetchDocxArrayBuffer, fetchDocxHtml } from "../api";

interface Props {
  docxUrl: string;
  productPrefix: string;
  sessionToken: string;
}

type State =
  | { kind: "idle" }
  | { kind: "loading"; label: string }
  | { kind: "ok"; html: string; source: "browser" | "server"; notes?: string }
  | { kind: "missing" }
  | { kind: "error"; message: string };

export function DocxPreview({ docxUrl, productPrefix, sessionToken }: Props) {
  const [state, setState] = useState<State>({ kind: "idle" });
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setState({ kind: "loading", label: "Downloading DOCX…" });

      let buf: ArrayBuffer | null = null;
      try {
        buf = await fetchDocxArrayBuffer(docxUrl);
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 404) {
          if (!cancelled) setState({ kind: "missing" });
          return;
        }
        if (!cancelled)
          setState({
            kind: "error",
            message: `Could not download DOCX (${String((e as Error).message)}).`,
          });
        return;
      }

      // Try browser-side conversion via mammoth (loaded from CDN in index.html).
      const mammothGlobal = (window as unknown as {
        mammoth?: {
          convertToHtml: (opts: { arrayBuffer: ArrayBuffer }) => Promise<{
            value: string;
            messages?: Array<{ message: string }>;
          }>;
        };
      }).mammoth;
      if (mammothGlobal) {
        try {
          setState({ kind: "loading", label: "Rendering in browser…" });
          const res = await mammothGlobal.convertToHtml({ arrayBuffer: buf });
          if (!cancelled) {
            setState({
              kind: "ok",
              html: res.value,
              source: "browser",
              notes:
                res.messages && res.messages.length
                  ? res.messages.map((m) => m.message).join("; ")
                  : undefined,
            });
          }
          return;
        } catch {
          // fall through to server
        }
      }

      try {
        setState({ kind: "loading", label: "Rendering on server…" });
        const html = await fetchDocxHtml(productPrefix, sessionToken);
        if (!cancelled) setState({ kind: "ok", html, source: "server" });
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (!cancelled) {
          if (status === 501) {
            setState({
              kind: "error",
              message:
                "Server-side DOCX conversion is unavailable — install the 'mammoth' Python package in the same env as uvicorn.",
            });
          } else {
            setState({
              kind: "error",
              message: `Server preview failed: ${String((e as Error).message)}`,
            });
          }
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [docxUrl, productPrefix, sessionToken]);

  if (state.kind === "idle" || state.kind === "loading") {
    return (
      <div className="warn-inline" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span className="spinner" aria-hidden style={{ color: "var(--accent)" }} />
        {state.kind === "loading" ? state.label : "Preparing preview…"}
      </div>
    );
  }

  if (state.kind === "missing") {
    return (
      <div className="warn-inline">
        No Word report was produced for this run. Use the JSON / YAML tabs for the same data,
        or check the server log — some scenarios do not emit a DOCX.
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className="warn-inline">
        <strong>Couldn't render DOCX inline.</strong>
        <br />
        {state.message}
        <br />
        Try{" "}
        <a href={docxUrl} download>
          downloading the file
        </a>{" "}
        and opening it in Word for exact layout.
      </div>
    );
  }

  return (
    <>
      <div className="toolbar">
        <span style={{ fontSize: 12, color: "var(--text-subtle)" }}>
          Rendered {state.source === "browser" ? "in browser" : "on server"} via mammoth — layout
          may differ slightly from Microsoft Word.
          {state.notes ? ` Notes: ${state.notes}` : ""}
        </span>
        <a className="btn ghost sm" href={docxUrl}>
          Download DOCX
        </a>
      </div>
      <div className="docx-canvas" dangerouslySetInnerHTML={{ __html: state.html }} />
    </>
  );
}
