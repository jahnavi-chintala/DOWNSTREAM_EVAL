import clsx from "clsx";
import type { MetricRow } from "../types";
import { extractNumeric, metricTone, prettifyMetricName } from "../lib/format";

export function MetricCards({ rows }: { rows: MetricRow[] }) {
  if (!rows || rows.length === 0) return null;

  const heroRow = rows.find((r) => r.hero);
  const regular = rows.filter((r) => !r.hero);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {heroRow && <OverallScoreHero row={heroRow} />}
      <div className="metrics-grid">
        {regular.map((r, i) => {
          const { short, rest } = prettifyMetricName(r.metric);
          const { value, unit } = extractNumeric(r.detail);
          const tone = r.na ? "" : metricTone(r);
          return (
            <div
              key={`${r.metric}-${i}`}
              className={clsx("metric-card", tone)}
              title={r.tooltip || undefined}
            >
              <div className="label">{short || "METRIC"}</div>
              <div className="value">
                {r.na ? (
                  <span style={{ fontSize: 16, color: "var(--text-muted)" }}>N/A</span>
                ) : (
                  <>
                    {value}
                    {unit ? (
                      <span
                        style={{
                          fontSize: 14,
                          color: "var(--text-muted)",
                          marginLeft: 3,
                          fontFamily: "var(--sans)",
                        }}
                      >
                        {unit}
                      </span>
                    ) : null}
                  </>
                )}
              </div>
              <div className="foot">
                <span className="name" title={r.tooltip || r.metric}>
                  {rest || r.metric}
                </span>
                <span
                  style={{
                    fontSize: 10.5,
                    letterSpacing: "0.1em",
                    textTransform: "uppercase",
                    color: r.na
                      ? "var(--text-subtle)"
                      : tone === "pass"
                      ? "var(--pass)"
                      : tone === "fail"
                      ? "var(--fail)"
                      : "var(--text-subtle)",
                  }}
                >
                  {r.na
                    ? "n/a"
                    : tone === "pass"
                    ? "pass"
                    : tone === "fail"
                    ? "fail"
                    : "—"}
                </span>
              </div>
              {r.tooltip ? (
                <div
                  style={{
                    marginTop: 8,
                    fontSize: 11,
                    color: "var(--text-subtle)",
                    lineHeight: 1.35,
                    borderTop: "1px dashed var(--border)",
                    paddingTop: 6,
                  }}
                >
                  {r.tooltip}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function OverallScoreHero({ row }: { row: MetricRow }) {
  const { value, unit } = extractNumeric(row.detail);
  const tone = metricTone(row);
  const toneColor =
    tone === "pass"
      ? "var(--pass)"
      : tone === "fail"
      ? "var(--fail)"
      : "var(--text-subtle)";
  return (
    <div
      className="panel"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 24,
        padding: "20px 24px",
        border: `1px solid ${toneColor}`,
        background:
          tone === "pass"
            ? "linear-gradient(135deg, rgba(22,163,74,0.05), rgba(22,163,74,0.02))"
            : tone === "fail"
            ? "linear-gradient(135deg, rgba(220,38,38,0.05), rgba(220,38,38,0.02))"
            : undefined,
      }}
    >
      <div style={{ flex: "0 0 auto" }}>
        <div
          style={{
            fontSize: 11,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            color: "var(--text-subtle)",
            fontWeight: 600,
          }}
        >
          Final composite score
        </div>
        <div
          style={{
            fontSize: 48,
            lineHeight: 1.05,
            fontWeight: 700,
            fontFamily: "var(--sans)",
            color: toneColor,
            marginTop: 4,
          }}
        >
          {value}
          {unit ? (
            <span style={{ fontSize: 22, color: "var(--text-muted)" }}>{unit}</span>
          ) : null}
        </div>
        <div
          style={{
            marginTop: 4,
            fontSize: 12,
            fontWeight: 600,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: toneColor,
          }}
        >
          {tone === "pass" ? "GO" : tone === "fail" ? "NO-GO" : "—"}
        </div>
      </div>
      {row.tooltip ? (
        <div
          style={{
            fontSize: 13,
            color: "var(--text-muted)",
            lineHeight: 1.45,
            maxWidth: 560,
          }}
        >
          {row.tooltip}
        </div>
      ) : null}
    </div>
  );
}
