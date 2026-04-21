import type { TracingBlock, TracingRow, TracingStatus } from "../types";

interface Props {
  tracing: TracingBlock;
}

const STATUS_COPY: Record<
  TracingStatus,
  { label: string; tone: "ok" | "warn" | "err" | "muted"; desc: string }
> = {
  id_match: {
    label: "id match",
    tone: "ok",
    desc: "usdm_id matched a USDM node id exactly.",
  },
  name_match: {
    label: "name match",
    tone: "ok",
    desc: "signal text matched a USDM node name for this instanceType.",
  },
  type_only: {
    label: "type only",
    tone: "warn",
    desc: "Claimed instanceType exists in USDM, but no node name matched the signal text.",
  },
  unresolved: {
    label: "unresolved",
    tone: "err",
    desc: "Reference did not resolve — claimed entity absent or signal doesn't match any node.",
  },
  no_usdm: {
    label: "no USDM",
    tone: "muted",
    desc: "No USDM JSON uploaded; tracing was skipped for this run.",
  },
};

function Chip({
  tone,
  children,
}: {
  tone: "ok" | "warn" | "err" | "muted";
  children: React.ReactNode;
}) {
  const bg =
    tone === "ok"
      ? "var(--chip-ok-bg)"
      : tone === "warn"
      ? "var(--chip-warn-bg)"
      : tone === "err"
      ? "var(--chip-err-bg)"
      : "rgba(15,23,42,0.06)";
  const fg =
    tone === "ok"
      ? "var(--chip-ok-fg)"
      : tone === "warn"
      ? "var(--chip-warn-fg)"
      : tone === "err"
      ? "var(--chip-err-fg)"
      : "var(--ink-2)";
  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 999,
        background: bg,
        color: fg,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: 0.2,
        textTransform: "uppercase",
      }}
    >
      {children}
    </span>
  );
}

export function TracingPanel({ tracing }: Props) {
  const counts = tracing.status_counts || ({} as Record<TracingStatus, number>);
  const rows = tracing.rows || [];
  const allStatuses: TracingStatus[] = [
    "id_match",
    "name_match",
    "type_only",
    "unresolved",
    "no_usdm",
  ];

  return (
    <section className="panel">
      <div className="panel-header">
        <h3>USDM tracing</h3>
        <span className="hint">
          {tracing.usdm_loaded
            ? `${tracing.usdm_node_count.toLocaleString()} USDM nodes indexed`
            : "USDM not uploaded"}
        </span>
      </div>
      <div className="panel-body">
        <p
          className="hint"
          style={{ marginTop: 0, marginBottom: 10, fontSize: 13 }}
        >
          Every generator claim referencing USDM is verified against the uploaded
          protocol USDM JSON. A reference counts as traced only if its{" "}
          <code>usdm_id</code> resolves in the USDM id index, or its{" "}
          <code>signal</code> matches the name/label/text of a USDM node whose
          <code> instanceType</code> equals the claimed <code>entity</code>.
        </p>

        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 8,
            margin: "10px 0 16px",
          }}
        >
          {allStatuses.map((s) => {
            const c = counts[s] ?? 0;
            if (c === 0 && s !== "id_match" && s !== "name_match") return null;
            const info = STATUS_COPY[s];
            return (
              <span
                key={s}
                style={{ display: "inline-flex", gap: 6, alignItems: "center" }}
                title={info.desc}
              >
                <Chip tone={info.tone}>{info.label}</Chip>
                <span style={{ fontSize: 13, color: "var(--ink-1)" }}>{c}</span>
              </span>
            );
          })}
        </div>

        {rows.length === 0 ? (
          <p className="hint" style={{ fontSize: 13 }}>
            No failing references — every USDM reference resolved cleanly.
          </p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table
              className="mt"
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontSize: 12.5,
              }}
            >
              <thead>
                <tr>
                  <th style={thStyle}>Status</th>
                  <th style={thStyle}>Field path</th>
                  <th style={thStyle}>Entity</th>
                  <th style={thStyle}>Signal</th>
                  <th style={thStyle}>usdm_id</th>
                  <th style={thStyle}>Nearest USDM candidates</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <TracingRowView key={i} row={r} />
                ))}
              </tbody>
            </table>
            {tracing.truncated && (
              <p className="hint" style={{ fontSize: 12 }}>
                Showing the first {rows.length} rows. Download the JSON artifact
                for the full list.
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function TracingRowView({ row }: { row: TracingRow }) {
  const info = STATUS_COPY[row.status] || STATUS_COPY.unresolved;
  const cands = row.candidates || [];
  return (
    <tr>
      <td style={tdStyle}>
        <Chip tone={info.tone}>{info.label}</Chip>
      </td>
      <td style={{ ...tdStyle, fontFamily: "var(--mono)", whiteSpace: "nowrap" }}>
        {row.field_path}
      </td>
      <td style={tdStyle}>{row.entity || "—"}</td>
      <td style={{ ...tdStyle, maxWidth: 360 }}>
        <div
          style={{
            maxHeight: 60,
            overflow: "auto",
            whiteSpace: "normal",
          }}
          title={row.signal}
        >
          {row.signal || "—"}
        </div>
      </td>
      <td style={tdStyle}>
        <code>{row.usdm_id || "—"}</code>
      </td>
      <td style={{ ...tdStyle, maxWidth: 340 }}>
        {cands.length === 0 ? (
          <span className="hint" style={{ fontSize: 12 }}>
            {row.reason || "—"}
          </span>
        ) : (
          <ol
            style={{
              margin: 0,
              paddingLeft: 18,
              fontSize: 12,
              lineHeight: 1.35,
            }}
          >
            {cands.slice(0, 3).map((c, i) => (
              <li key={i}>
                <code>{c.id || "(no id)"}</code> — {c.name}
              </li>
            ))}
          </ol>
        )}
      </td>
    </tr>
  );
}

const thStyle: React.CSSProperties = {
  borderBottom: "1px solid var(--rule)",
  textAlign: "left",
  padding: "6px 8px",
  background: "var(--surface-2)",
  fontWeight: 600,
  color: "var(--ink-1)",
};

const tdStyle: React.CSSProperties = {
  borderBottom: "1px solid var(--rule-soft)",
  padding: "6px 8px",
  verticalAlign: "top",
};
