import type { EvalPreview, MetricRow } from "../types";

export function verdictTone(v: string | null | undefined): "pass" | "warn" | "fail" | "quiet" {
  if (!v) return "quiet";
  const s = String(v).trim().toUpperCase();
  if (/(^|\s)(GO|PASS|GREEN)(\s|$)/.test(s)) return "pass";
  if (/NO[- ]?GO|FAIL|RED/.test(s)) return "fail";
  if (/WARN|AMBER|YELLOW|REVIEW/.test(s)) return "warn";
  return "quiet";
}

export function metricTone(row: MetricRow): "pass" | "fail" | "warn" | "quiet" {
  if (row.pass === true) return "pass";
  if (row.pass === false) return "fail";
  const detail = (row.detail || "").toUpperCase();
  if (detail.includes("SKIP")) return "quiet";
  if (detail.includes("WARN")) return "warn";
  return "quiet";
}

export function prettifyMetricName(name: string): { short: string; rest: string } {
  const t = (name || "").trim();
  const m = t.match(/^([A-Z]?\d+[A-Za-z]?)\s+(.*)$/);
  if (m) return { short: m[1], rest: m[2] };
  const parts = t.split(/\s+/);
  if (parts.length > 1) return { short: parts[0], rest: parts.slice(1).join(" ") };
  return { short: t, rest: "" };
}

export function extractNumeric(detail: string): { value: string; unit?: string } {
  const t = (detail || "").trim();
  if (!t) return { value: "—" };
  const pct = t.match(/([0-9]+(?:\.[0-9]+)?)\s*%/);
  if (pct) return { value: pct[1], unit: "%" };
  const num = t.match(/^(-?[0-9]+(?:\.[0-9]+)?)$/);
  if (num) return { value: num[1] };
  if (/^SKIP$/i.test(t)) return { value: "—", unit: "skip" };
  if (/^PASS$/i.test(t)) return { value: "pass" };
  if (/^FAIL$/i.test(t)) return { value: "fail" };
  return { value: t };
}

export function titleCase(s: string): string {
  return s
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function summariseCounts(counts: Record<string, unknown> | undefined): string {
  if (!counts) return "";
  return Object.entries(counts)
    .map(([k, v]) => `${titleCase(k)}: ${String(v)}`)
    .join(" · ");
}

export function resolveHeadlineScore(p: EvalPreview | null | undefined): string | null {
  if (!p?.headline) return null;
  const h = p.headline;
  if (
    typeof h.overall_score_percent === "number" &&
    Number.isFinite(h.overall_score_percent)
  ) {
    return `${h.overall_score_percent.toFixed(1)}%`;
  }
  if (h.document_score != null && h.document_score !== "") return String(h.document_score);
  return null;
}
