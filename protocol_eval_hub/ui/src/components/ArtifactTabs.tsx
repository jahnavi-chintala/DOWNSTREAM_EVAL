import { useEffect, useState } from "react";
import yaml from "js-yaml";
import clsx from "clsx";
import type { EvalDownloads } from "../types";
import { fetchJsonArtifact, fetchTextArtifact } from "../api";
import { CodePanel } from "./CodePanel";
import { DocxPreview } from "./DocxPreview";

type TabKey = "docx" | "json" | "yaml";

interface Props {
  downloads: EvalDownloads;
  productPrefix: string;
  sessionToken: string;
}

interface ArtifactState {
  jsonText: string;
  jsonRaw: unknown;
  yamlText: string;
  yamlStatus: "loading" | "ok" | "missing" | "error";
  yamlMessage?: string;
  jsonStatus: "loading" | "ok" | "error";
  jsonMessage?: string;
}

export function ArtifactTabs({ downloads, productPrefix, sessionToken }: Props) {
  const [tab, setTab] = useState<TabKey>("docx");
  const [s, setS] = useState<ArtifactState>({
    jsonText: "",
    jsonRaw: null,
    yamlText: "",
    yamlStatus: "loading",
    jsonStatus: "loading",
  });

  useEffect(() => {
    let cancelled = false;
    async function loadJson() {
      try {
        const raw = await fetchJsonArtifact(downloads.json);
        const txt = JSON.stringify(raw, null, 2);
        if (!cancelled)
          setS((prev) => ({
            ...prev,
            jsonText: txt,
            jsonRaw: raw,
            jsonStatus: "ok",
          }));
      } catch (e) {
        if (!cancelled)
          setS((prev) => ({
            ...prev,
            jsonStatus: "error",
            jsonMessage: (e as Error).message,
          }));
      }
    }
    async function loadYaml() {
      try {
        const txt = await fetchTextArtifact(downloads.yaml);
        if (!cancelled)
          setS((prev) => ({ ...prev, yamlText: txt, yamlStatus: "ok" }));
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (!cancelled) {
          if (status === 404) {
            setS((prev) => ({ ...prev, yamlStatus: "missing" }));
          } else {
            setS((prev) => ({
              ...prev,
              yamlStatus: "error",
              yamlMessage: (e as Error).message,
            }));
          }
        }
      }
    }
    setS({
      jsonText: "",
      jsonRaw: null,
      yamlText: "",
      yamlStatus: "loading",
      jsonStatus: "loading",
    });
    loadJson();
    loadYaml();
    return () => {
      cancelled = true;
    };
  }, [downloads.json, downloads.yaml]);

  // Fallback: build YAML from JSON in the browser if the server didn't produce one.
  const yamlRender =
    s.yamlStatus === "ok"
      ? s.yamlText
      : s.jsonStatus === "ok"
      ? (() => {
          try {
            return yaml.dump(s.jsonRaw, { lineWidth: 100, noRefs: true });
          } catch {
            return "";
          }
        })()
      : "";

  const yamlPlaceholder =
    s.yamlStatus === "missing"
      ? "No native YAML file for this run. Rendered from JSON in the browser — identical data."
      : s.yamlStatus === "error"
      ? `Could not load YAML: ${s.yamlMessage ?? "unknown error"}.`
      : "";

  return (
    <div className="tabs-wrap">
      <div className="tabs-head" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "docx"}
          className={clsx(tab === "docx" && "on")}
          onClick={() => setTab("docx")}
        >
          Report <span className="badge">DOCX</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "json"}
          className={clsx(tab === "json" && "on")}
          onClick={() => setTab("json")}
        >
          JSON
          {s.jsonRaw ? (
            <span className="badge">{Object.keys(s.jsonRaw as object).length} keys</span>
          ) : null}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "yaml"}
          className={clsx(tab === "yaml" && "on")}
          onClick={() => setTab("yaml")}
        >
          YAML
          {s.yamlStatus === "missing" ? <span className="badge">derived</span> : null}
        </button>
      </div>

      <div className={clsx("tab-panel", tab === "docx" && "on")} role="tabpanel">
        {tab === "docx" && (
          <DocxPreview
            docxUrl={downloads.docx}
            productPrefix={productPrefix}
            sessionToken={sessionToken}
          />
        )}
      </div>

      <div className={clsx("tab-panel", tab === "json" && "on")} role="tabpanel">
        {tab === "json" && s.jsonStatus === "loading" && (
          <div className="warn-inline">Loading JSON…</div>
        )}
        {tab === "json" && s.jsonStatus === "error" && (
          <div className="warn-inline">Could not load JSON: {s.jsonMessage}</div>
        )}
        {tab === "json" && s.jsonStatus === "ok" && (
          <CodePanel text={s.jsonText} language="json" downloadUrl={downloads.json} />
        )}
      </div>

      <div className={clsx("tab-panel", tab === "yaml" && "on")} role="tabpanel">
        {tab === "yaml" && (
          <CodePanel
            text={yamlRender}
            language="yaml"
            downloadUrl={downloads.yaml}
            placeholder={yamlPlaceholder}
          />
        )}
      </div>
    </div>
  );
}
