import type { EvalResponse, RecentRun } from "../types";

const KEY = "protocol-eval-recent-runs";
const MAX = 20;

export function loadRecent(): RecentRun[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as RecentRun[];
    return Array.isArray(parsed) ? parsed.slice(0, MAX) : [];
  } catch {
    return [];
  }
}

export function addRecent(res: EvalResponse): RecentRun[] {
  const h = res.preview?.headline;
  const entry: RecentRun = {
    product: res.product,
    study_id: res.study_id,
    verdict: res.verdict || (h?.verdict ?? "") + "",
    session_token: res.session_token,
    downloads: res.downloads,
    ran_at: res.ran_at,
    headline_score: h?.document_score ?? null,
  };
  const current = loadRecent().filter((r) => r.session_token !== entry.session_token);
  const next = [entry, ...current].slice(0, MAX);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // storage full / blocked, ignore
  }
  return next;
}

export function clearRecent(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}

export function formatWhen(iso: string): string {
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const min = Math.round(diff / 60000);
    if (min < 1) return "just now";
    if (min < 60) return `${min} min ago`;
    const hr = Math.round(min / 60);
    if (hr < 24) return `${hr}h ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}
