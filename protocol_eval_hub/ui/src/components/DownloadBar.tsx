import type { EvalDownloads } from "../types";

interface Props {
  downloads: EvalDownloads;
  docHint?: string;
}

export function DownloadBar({ downloads, docHint }: Props) {
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 8,
        justifyContent: "space-between",
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <a className="btn primary" href={downloads.zip}>
          Download ZIP bundle
        </a>
        <a className="btn ghost sm" href={downloads.docx}>
          DOCX
        </a>
        <a className="btn ghost sm" href={downloads.json}>
          JSON
        </a>
        <a className="btn ghost sm" href={downloads.yaml}>
          YAML
        </a>
      </div>
      {docHint && (
        <span style={{ fontSize: 11.5, color: "var(--text-subtle)", maxWidth: "42ch" }}>
          {docHint}
        </span>
      )}
    </div>
  );
}
