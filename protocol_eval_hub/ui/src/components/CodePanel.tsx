import { useMemo, useState } from "react";

interface Props {
  text: string;
  language: "json" | "yaml";
  downloadUrl: string;
  placeholder?: string;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function highlight(text: string, q: string): string {
  const safe = escapeHtml(text);
  if (!q.trim()) return safe;
  const re = new RegExp(
    q.trim().replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&"),
    "gi"
  );
  return safe.replace(re, (m) => `<mark>${m}</mark>`);
}

export function CodePanel({ text, language, downloadUrl, placeholder }: Props) {
  const [q, setQ] = useState("");
  const [copied, setCopied] = useState(false);

  const html = useMemo(() => highlight(text || "", q), [text, q]);
  const lines = (text || "").split("\n").length;

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // ignore
    }
  }

  if (!text) {
    return (
      <div className="warn-inline">
        {placeholder || `No ${language.toUpperCase()} content available.`}
      </div>
    );
  }

  return (
    <>
      <div className="toolbar">
        <input
          className="q"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={`Search in ${language.toUpperCase()} (${lines} lines)`}
          aria-label={`Search in ${language}`}
        />
        <button type="button" className="btn ghost sm" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
        <a className="btn ghost sm" href={downloadUrl}>
          Download
        </a>
      </div>
      <pre className="code" dangerouslySetInnerHTML={{ __html: html }} />
    </>
  );
}
